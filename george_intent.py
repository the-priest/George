#!/usr/bin/env python3
# -*- coding: ascii -*-
"""
george_intent.py -- the router.

THE PROBLEM THIS SOLVES

George's agent loop makes the model do work a lookup table can do. Ask
"what's the weather?" and the old path was:

    call 1  model reads 1800 tokens of prompt, emits {"tool":"weather"}
    ---     George runs the weather tool
    call 2  model reads prompt + observation, emits {"tool":"answer"}

Two full model round trips, on CPU, to answer a question whose tool was
never in doubt. Half the latency of a typical turn is the model
deciding something that was already decided by the words he used.

The router recognises the obvious cases up front and RUNS THE TOOLS
BEFORE the model is called at all. The model then gets one call, with
the observations already in hand, and only has to write the reply:

    call 1  model reads prompt + observations, emits the answer

One round trip instead of two or three. Nothing is taken away -- the
model can still call any tool afterwards if the router guessed short.

DESIGN RULES

  * Deterministic. Regex and keywords, no model, no network. Routing
    must never be the slow part.
  * Conservative. A rule fires only when the phrasing is unambiguous.
    A miss costs one extra round trip -- exactly what happens today.
    A WRONG prefetch wastes a tool run and pollutes the context, which
    is worse. When in doubt, do not route.
  * Additive. The router only ever PRE-runs tools. It never answers for
    the model, never suppresses a tool the model asks for, and never
    stops the loop early. The one exception is CHAT, which asserts only
    that NO tool is needed.
  * GTK-free and side-effect-free at import. Everything here is pure
    text -> plan, so the whole thing is testable headless.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, NamedTuple, Optional, Tuple


class Plan(NamedTuple):
    """What the router decided.

    name      the rule that fired, for logging and tests
    prefetch  [(tool, args)] to run BEFORE the first model call
    hint      one line appended to the turn, telling the model what it
              has already been given and what is expected of it
    chat      True when this needs no tools at all
    """
    name: str
    prefetch: List[Tuple[str, Dict[str, Any]]]
    hint: str
    chat: bool = False


# =====================================================================
# NORMALISATION
# =====================================================================

_PUNCT = re.compile(r"[^\w\s:/.@-]+")
_WS = re.compile(r"\s+")

# Contractions are folded BEFORE punctuation is stripped. Otherwise
# "how's" becomes "how s" -- two tokens with a space in the middle -- and
# every rule written as `how'?s` silently stops matching. That was
# costing the router most of its hit rate.
_CONTRACTIONS = re.compile(
    r"\b(what|how|who|where|when|that|there|here|it|he|she|let|"
    r"whats|hows)'s\b")


def normalise(text: str) -> str:
    """Lowercase, fold contractions, strip punctuation, collapse space.

    Keeps : / . @ - so URLs and paths survive for the rules that look
    for them.

    NOTE: normalised text is for MATCHING ONLY. Never carry a value out
    of it into a tool argument that has to be exact -- see _raw_url.
    """
    s = str(text or "").lower()
    s = s.replace("\u2019", "'")                 # smart apostrophe
    s = _CONTRACTIONS.sub(lambda m: m.group(1) + "s", s)
    s = s.replace("n't", "nt").replace("'re", "re").replace("'ll", "ll")
    s = _WS.sub(" ", _PUNCT.sub(" ", s)).strip()
    return s


# =====================================================================
# URLS -- taken from the RAW text, never the normalised text
#
# normalise() lowercases and strips punctuation, which is right for
# matching and catastrophic for a URL. It turned
#
#     https://youtu.be/dQw4w9WgXcQ?t=43
# into
#     https://youtu.be/dqw4w9wgxcq
#
# -- a 404, because a YouTube id is case-sensitive and the query string
# is not decoration. Every "open this link" with a capital letter, a
# query string or a fragment in it opened the wrong page. So the verb is
# matched against the normalised text and the URL is lifted out of the
# ORIGINAL string, untouched.
# =====================================================================

_RAW_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"'`]+", re.I)
_URL_TAIL = ".,;:!?)]}'\"*_"


def _raw_url(raw: str) -> str:
    """The first URL in the text he actually typed, byte for byte."""
    m = _RAW_URL.search(str(raw or ""))
    if not m:
        return ""
    url = m.group(0)
    # Sentence punctuation clings to the end of a pasted link. Trailing
    # `)` only counts as punctuation if it is unbalanced -- wikipedia
    # article titles genuinely end in one.
    while url and url[-1] in _URL_TAIL:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


_OPEN_VERB = re.compile(r"\b(open|show|pull up|bring up|put|launch|load|"
                        r"go to|visit|take me to)\b")

_LIST_VERB = re.compile(
    r"\b(list (the )?(files|contents)|whats in|what is in|contents of|"
    r"what have i got in|whats inside|ls)\b")

SHOW_HINT = ("The observation above says whether it actually opened. "
             "Report exactly that - if it failed, say it failed and why. "
             "Never claim it is on his screen unless the observation says "
             "it opened.")

# READING a page is not OPENING it. "read me that article" wants the
# text fetched and summarised; "open that article" wants it on screen.
# They used to be the same rule, so one of the two was always wrong.
_READ_VERB = re.compile(r"\b(read|summarise|summarize|what does .{0,30}say|"
                        r"whats (on|in) (that|this) (page|article|link))\b")

READ_HINT = ("The page text is in the observation above. Answer from it "
             "and say what the page is. Nothing was put on his screen - "
             "do not say it was.")


# =====================================================================
# PATHS -- also taken from the RAW text, and for the same reason
#
# normalise() does not keep `~`, so "open ~/projects" arrived as "open
# projects". Paths are case-sensitive on his filesystem too.
# =====================================================================

_RAW_PATH = re.compile(r"(?<!\S)(?:~|\.{1,2})?/[^\s'\"]*|(?<!\S)~(?!\S)")

# The folders he refers to by name rather than by path.
_FOLDERS = {
    "downloads": "~/Downloads", "download": "~/Downloads",
    "documents": "~/Documents", "docs": "~/Documents",
    "desktop": "~/Desktop", "pictures": "~/Pictures",
    "photos": "~/Pictures", "music": "~/Music", "videos": "~/Videos",
    "home": "~", "home directory": "~", "home folder": "~",
}
_FOLDER_WORD = re.compile(
    r"\b(?:my |the )?(?P<f>downloads?|documents|docs|desktop|pictures|"
    r"photos|music|videos|home)\b(?: (?:folder|directory|dir))?")


# =====================================================================
# ARITHMETIC -- raw text again, for the third time and the same reason
#
# normalise() strips + * % ( ), so "calculate 15*23" reached a rule as
# "calculate 15 23". Anything the router carries into a tool argument
# has to come out of the ORIGINAL string; the normalised text is for
# deciding WHICH rule fires and nothing else.
#
# calc is worth routing because a 4B doing mental arithmetic is a coin
# flip, and calc is a pure function with no network and no side effects.
# Running it up front costs nothing, removes a wrong answer, and saves a
# round trip.
# =====================================================================

_CALC_ASK = re.compile(
    r"\b(?:calculate|calc|work out|what(?:'?s| is)|how much is)\s+"
    r"(?P<e>[\d(][\d\s+*/%^().-]*)", re.I)
_CALC_OK = re.compile(r"^[\d\s+*/%^().-]+$")
_CALC_OP = re.compile(r"[+*/%^-]")


def _calc_expression(raw: str) -> str:
    """An arithmetic expression he asked for, or ''.

    Conservative on purpose: it must be nothing but numbers and
    operators, it must contain an operator, and it must end on a number
    or a closing bracket. "how much is 17% of 4300" is not arithmetic
    this can do, and saying so by declining is better than guessing.
    """
    m = _CALC_ASK.search(str(raw or ""))
    if not m:
        return ""
    expr = m.group("e").strip()
    if not expr or not _CALC_OK.match(expr) or not _CALC_OP.search(expr):
        return ""
    if expr[-1] not in "0123456789)":
        return ""
    if expr.count("(") != expr.count(")"):
        return ""
    return expr.replace("^", "**")


_FIND_ASK = re.compile(
    r"\b(?:find|locate)\s+(?:me\s+)?(?:the\s+|a\s+|any\s+)*files?\b"
    r".{0,20}?\b(?:called|named|matching|like)\s+"
    r"(?P<p>[\w.*?/\[\]-]{2,60})", re.I)


def _raw_path(raw: str, norm: str) -> str:
    """A filesystem path from the message, or ''.

    An explicit path wins and is taken from the raw string. Failing
    that, a folder he named in words is mapped to the usual place.
    """
    m = _RAW_PATH.search(str(raw or ""))
    if m:
        hit = m.group(0).rstrip(".,;:!?")
        # A URL is not a path, and is handled before this is reached.
        if hit and not hit.startswith("//"):
            return hit
    m = _FOLDER_WORD.search(norm or "")
    if m:
        return _FOLDERS.get(m.group("f"), "")
    return ""


# =====================================================================
# PLACES -- a weather rule that does not look up "like today"
#
# The rule used to hand everything after the keyword to the weather tool
# as a location, so "what's the weather like today" asked wttr.in about
# a place called "like today" and "forecast for the weekend" asked about
# "the weekend". A location is only accepted when a preposition
# introduces it and what follows actually reads like a place name;
# otherwise the location is left blank, which means "where he is" and is
# the right answer to an unqualified question anyway.
# =====================================================================

_WHERE_INTRO = re.compile(r"\b(?:in|at|near|around|for|over)\s+(?P<p>.+)$")

# Tokens that end a place name. A place never continues past one of
# these, and a place made only of these is not a place.
_NOT_PLACE = frozenset((
    "like", "today", "tomorrow", "tonight", "morning", "afternoon",
    "evening", "weekend", "week", "month", "later", "now", "right",
    "this", "next", "last", "please", "cheers", "ta", "thanks",
    "and", "or", "but", "so", "then", "going", "gonna", "be", "is",
    "it", "do", "i", "me", "my", "you", "your", "need", "coat",
    "jacket", "umbrella", "outside", "out", "home", "here", "there",
    "weather", "forecast", "temperature", "rain", "snow", "hot", "cold",
    "warm", "day", "days", "hours", "hour", "bit", "while",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday",
))

# What survives has to read like a place: letters, spaces and the
# punctuation real place names use. "my run at 5" does not.
_PLACE_OK = re.compile(r"^[a-z][a-z .'-]{1,40}$")


def _place_from(tail: str) -> str:
    """A location from the tail of a weather question, or ''."""
    m = _WHERE_INTRO.search(str(tail or ""))
    if not m:
        return ""
    words = m.group("p").split()
    kept: List[str] = []
    for w in words:
        if w in _NOT_PLACE:
            break
        kept.append(w)
    while kept and kept[-1] in ("the", "a", "an", "of"):
        kept.pop()
    place = " ".join(kept).strip()
    if len(kept) > 4 or not _PLACE_OK.match(place):
        return ""
    return place


# =====================================================================
# CHAT -- needs no tool at all
# =====================================================================

_GREETINGS = frozenset((
    "hi", "hey", "hello", "yo", "sup", "howdy", "hiya", "morning",
    "good morning", "good afternoon", "good evening", "good night",
    "night", "hi there", "hey there", "hello there", "hey george",
    "hi george", "hello george", "yo george", "george",
))

_PLEASANTRIES = frozenset((
    "thanks", "thank you", "ta", "cheers", "nice one", "good one",
    "ok", "okay", "cool", "nice", "great", "lovely", "grand", "sound",
    "no worries", "np", "sorry", "my bad", "never mind", "nevermind",
    "forget it", "bye", "goodbye", "see ya", "later", "gn", "lol",
    "haha", "nice work", "well done", "good job", "perfect", "awesome",
))

# "how are you" and friends: about GEORGE, not about the machine. The
# distinction matters because "how are you" and "how is the box" used to
# route to the same place.
_ABOUT_GEORGE = re.compile(
    r"^(how (are|r) (you|u)|hows it going|how are things|you (ok|good|"
    r"alright)|what'?s up|whats up|wassup|who are you|what are you|"
    r"what can you do|what do you do)\b")

# "help" was an alternative inside _ABOUT_GEORGE above, anchored with
# \b -- so "help me write a bash script" was classified as small talk
# and the model was told, in as many words, not to use a tool. A bare
# "help" is a question about George; "help me <do a thing>" is the
# opposite of one. Only the bare forms count.
_HELP_ASKS = frozenset((
    "help", "help me", "halp", "what can you help with",
    "what can you help me with", "what do you do for me",
))


def _is_chat(norm: str) -> Optional[str]:
    if not norm:
        return None
    if norm in _HELP_ASKS:
        return "chat.about-self"
    if norm in _GREETINGS:
        return "chat.greeting"
    if norm in _PLEASANTRIES:
        return "chat.pleasantry"
    if _ABOUT_GEORGE.match(norm):
        return "chat.about-self"
    return None


# =====================================================================
# RULES
#
# Each rule is (name, pattern, builder). The builder takes the match and
# the normalised text and returns (prefetch, hint), or None to decline
# after a closer look.
# =====================================================================

def _weather_args(m: "re.Match", norm: str) -> Tuple[List, str]:
    tail = ""
    if "where" in (m.groupdict() or {}):
        tail = m.group("where") or ""
    where = _place_from(tail)
    return ([("weather", {"location": where})],
            "The current conditions are in the observation above. Give "
            "him the answer in a sentence or two - what it is doing and "
            "whether he needs a coat. Do not call the weather tool again.")


_RULES: List[Tuple[str, "re.Pattern", Any]] = [

    # ---- opening a URL is NOT in this table --------------------------
    # It is handled in route() BEFORE normalisation, because a URL
    # cannot survive normalise(). See _raw_url and SHOW_HINT.

    # ---- weather -----------------------------------------------------
    # The tail is captured but a LOCATION only comes out of it when a
    # preposition introduces something that reads like a place -- see
    # _place_from. It used to be handed over whole, so "what's the
    # weather like today" asked wttr.in about a place called "like
    # today".
    ("weather", re.compile(
        r"\b(weather|forecast|temperature|how (hot|cold|warm|wet|windy)|"
        # "is it raining", "will it snow", and the "going to" forms of
        # both, which are how he actually asks
        r"(is|will) it "
        r"((going to |gonna )?(be )?)?"
        r"(rain|snow|hail|sleet|hot|cold|warm|wet|dry|windy|freezing|"
        r"mild|sunny|nice|miserable)\w*|"
        r"do i need (a|an) (coat|jacket|umbrella|brolly))\b(?P<where>.*)?"),
     _weather_args),

    # ---- this machine ------------------------------------------------
    # Deliberately NOT matching a bare "how are you" -- see _ABOUT_GEORGE.
    ("system", re.compile(
        r"\b((how(s| is| are)? (the |this |my )?(box|machine|laptop|"
        r"system|pc|computer|thinkpad)\b)|system (status|info|"
        r"information)|vitals|(cpu|ram|memory) (usage|load)|"
        r"how much (ram|memory)|what (os|distro|kernel|cpu|gpu)|"
        r"uptime|battery( level| status| percentage)?)\b"),
     lambda m, n: ([("system", {})],
                   "His machine's vitals are in the observation above. "
                   "Answer from them directly. Do not call the system "
                   "tool again.")),

    # ---- disk --------------------------------------------------------
    ("disk", re.compile(
        r"\b(disk (space|usage|full)|free space|space left|"
        r"how (much|full) .{0,12}(disk|drive|storage)|"
        r"(running )?out of space|storage)\b"),
     lambda m, n: ([("disk", {})],
                   "Filesystem usage is in the observation above. Tell "
                   "him what is full and what is worth clearing.")),

    # ---- what is eating the box --------------------------------------
    ("diagnose", re.compile(
        r"\b((whats|what is) wrong with (the|my) (box|machine|laptop)|"
        r"why (is|s) (it|the box|my box|this thing) (so )?(slow|sluggish|"
        r"laggy|crawling)|"
        r"(full|health) check|check (the|my) (box|machine|system)|"
        r"is everything (ok|alright|fine)|diagnose)\b"),
     lambda m, n: ([("diagnose", {})],
                   "A full health check is in the observation above, "
                   "with the verdict already worked out from the "
                   "numbers. Give him the verdict and the one thing "
                   "responsible. Do not call system, disk or processes.")),

    ("processes", re.compile(
        r"\b((what(s| is)? (eating|using|hogging|chewing)"
        r"( up)?( the| my)? (cpu|ram|memory|box|machine))|"
        r"top processes|biggest process|why (is|s) (it|the box|my box) "
        r"(so )?(slow|sluggish|laggy))\b"),
     lambda m, n: ([("system", {}),
                    ("processes", {"sort": "memory" if "mem" in n or "ram"
                                   in n else "cpu"})],
                   "Vitals and the heaviest processes are in the "
                   "observations above. Name the actual culprit and what "
                   "he can do about it.")),

    # ---- packages: one call, right dialect, no partial-upgrade risk ---
    # "search for X" USED TO BE HERE and this rule sits above the web
    # search rule, so "search for quantum computing" ran `pacman -Ss
    # quantum` and "search for the best pizza in dublin" ran `pacman -Ss
    # best`. A bare search verb says nothing about packages. The verb
    # now has to be `install`, or the word "package" has to be in the
    # sentence; everything else falls through to the web.
    ("pkg", re.compile(
        r"\b(is (?P<p1>[\w.+-]{2,40}) installed|"
        r"do i have (?P<p2>[\w.+-]{2,40}) installed|"
        r"(install|reinstall) (the )?(package )?(?P<p3>[\w.+-]{2,40})|"
        r"(search|look) for (the )?package (?P<p4>[\w.+-]{2,40}))\b"),
     lambda m, n: ([("pkg", {
         # READ-ONLY ONLY. The router runs this BEFORE the model has
         # decided anything, so it must never take an action that
         # changes the machine. "install ripgrep" prefetches a SEARCH;
         # the model then proposes the install and he confirms it, which
         # is the path every state change has to take.
         "action": "installed" if "installed" in n else "search",
         "package": (m.group("p1") or m.group("p2") or m.group("p3")
                     or m.group("p4") or "")})],
                   "The package manager result is in the observation "
                   "above, run in the correct dialect for this machine. "
                   "Answer from it. If he wants it installed and it is "
                   "not there yet, say what the install command would be "
                   "and let him confirm.")),

    # ---- network -----------------------------------------------------
    ("network", re.compile(
        r"\b(network (status|info)|my ip|ip address|wifi|wi-fi|"
        r"am i (online|connected)|internet (working|up|down)|"
        r"what network|which wifi)\b"),
     lambda m, n: ([("network", {})],
                   "Network state is in the observation above. Answer "
                   "from it.")),

    # ---- news --------------------------------------------------------
    ("news", re.compile(
        r"\b(news|headlines|what(s| is) (happening|going on|new)|"
        r"current events|latest stories|anything happening)\b"),
     lambda m, n: ([("news", {})],
                   "Headlines are in the observation above. Summarise "
                   "the few that matter in plain sentences. Read the "
                   "observation carefully: if feeds failed, say so and "
                   "name them, and do NOT claim anything is on his "
                   "screen unless a show tool said it opened.")),

    # ---- the daily brief: one phrase, three tools ---------------------
    ("brief", re.compile(
        r"\b(brief me|catch me up|what did i miss|"
        r"(morning|daily) (brief|briefing|rundown)|"
        r"(give me|whats) the rundown|status report|"
        r"how(s| is) everything|sitrep)\b"),
     lambda m, n: ([("system", {}), ("weather", {"location": ""}),
                    ("news", {})],
                   "His machine, the weather and the headlines are all "
                   "in the observations above. Give him a short brief: "
                   "the box first, then the weather in one line, then "
                   "two or three headlines that matter. Do not call any "
                   "of those tools again.")),

    # ---- time and date -----------------------------------------------
    # The CONTEXT block carries HIS clock, so this rule may only fire
    # for a question about HIS clock. "what's the time in tokyo" matched
    # it and got answered with Irish local time, confidently and wrongly.
    # A named place means decline, and the model works it out properly.
    ("clock", re.compile(
        r"^(what(s| is)? (the )?(time|date)|what day is it|"
        r"time please|whats today)\b(?P<tail>.*)$"),
     lambda m, n: None if re.search(r"\b(in|at|for|over)\s+[a-z]",
                                    m.group("tail") or "")
     else ([],
           "The current date and time are in your CONTEXT block. "
           "Answer directly - no tool is needed.")),

    # "arithmetic" and "what is in <folder>" are NOT here: like the URL
    # and path rules they need the raw string, so they live in route().

    # ---- find a file (read-only) -------------------------------------
    # "find files called X" is NOT here either, and for the sharpest
    # version of the same reason: normalise() strips `*` and `?`, so a
    # glob was silently un-globbed on the way to the tool.

    # ---- a factual question about the world ----------------------------
    # These are exactly the questions a 4B answers confidently and
    # wrongly from memory. Prefetching the reference means the evidence
    # is already in front of it when it writes the sentence.
    ("lookup", re.compile(
        r"^(who (is|was|were)|what (is|was|are|were)|whats|"
        r"when (did|was|is)|where (is|was)|tell me about)"
        # The article must be FOLLOWED BY SPACE or it eats the first
        # letter of the subject: "who is ada lovelace" matched the "a"
        # of "ada" and looked up "da lovelace".
        r"\s+(?:(?:a|an|the)\s+)?(?P<t>.{2,80}?)\??$"),
     lambda m, n: ([("lookup", {"term": m.group("t").strip()})],
                   "A reference article is in the observation above. "
                   "Answer from it and say where it came from. If it "
                   "does not actually answer him, say so rather than "
                   "filling the gap from memory.")),

    # ---- explicit web search ------------------------------------------
    ("search", re.compile(
        r"^(search( the web)?( for)?|google|look up|find out about|"
        r"what do (you|they) know about)\s+(?P<q>.{3,120})$"),
     lambda m, n: ([("web_search", {"query": m.group("q").strip()})],
                   "Search results are in the observation above. Answer "
                   "from them and say which source you used. If they do "
                   "not actually answer him, say so rather than "
                   "guessing.")),

    # ---- memory recall ------------------------------------------------
    ("recall", re.compile(
        r"\b(what do you (know|remember) about me|"
        r"what have you remembered|your memory|what do you recall|"
        r"what did i (ask you to remember|tell you)|"
        r"do you remember (anything|what)|what have i told you)\b"),
     lambda m, n: ([("recall", {})],
                   "What you have stored about him is in the observation "
                   "above. Answer from it, and say plainly if it is "
                   "empty.")),
]


# =====================================================================
# ROUTING
# =====================================================================

# Phrasings that mean "do not prefetch": he is asking about capability
# or giving an instruction about future behaviour, not asking for the
# thing now. Prefetching on these wastes a tool run and confuses the
# reply.
_HYPOTHETICAL = re.compile(
    r"\b(can you|could you|are you able|do you know how|how (do|would) "
    r"you|what if|next time|from now on|remember to|instead of|"
    r"why did you|why do you|you (said|told|claimed))\b")

# ...unless it is plainly a polite request for the thing right now.
# Phrasings unambiguous enough to survive the length guard below.
_ANCHORED = re.compile(
    r"(https?://|www\.)|^\s*(search|google|look up)\b")

_POLITE_NOW = re.compile(
    r"\b(can you|could you)\s+(please\s+)?(tell me|show me|give me|get|"
    r"pull|check|grab|find)\b")


def route(text: str, enabled: bool = True) -> Optional[Plan]:
    """Decide what to run before the model is called.

    Returns None when nothing is obvious, which is the normal case for
    anything interesting -- the loop then behaves exactly as it always
    has.
    """
    if not enabled:
        return None
    raw = str(text or "").strip()
    if not raw or len(raw) > 400:
        # A long message is a conversation, not a command. Let the model
        # read it properly.
        return None

    norm = normalise(raw)

    # ---- "open <url>" -- FIRST, and off the RAW string ---------------
    # First because a URL can contain any keyword: "open
    # https://news.ycombinator.com" used to match the NEWS rule and pull
    # RSS feeds instead of opening the page he named.
    #
    # Raw because normalise() lowercases and strips punctuation, so the
    # URL that reached the show tool was not the URL he typed --
    # ...youtu.be/dQw4w9WgXcQ?t=43 arrived as ...youtu.be/dqw4w9wgxcq,
    # which is a 404. Match the VERB against the normalised text, take
    # the URL from the original.
    url = _raw_url(raw)
    if url:
        # READ it (fetch the text) or OPEN it (put it on his screen)?
        # Checked in that order: "read me that page" also contains no
        # open verb, but "open and read" should read.
        if _READ_VERB.search(norm):
            return Plan("read", [("open_page", {"url": url})], READ_HINT)
        if _OPEN_VERB.search(norm):
            return Plan("show", [("show", {"url": url})], SHOW_HINT)

    # ---- "open <path>" -- same raw-string treatment ------------------
    # normalise() eats `~`, so "open ~/projects" used to arrive as
    # "open projects" and matched nothing at all.
    if not url:
        if _OPEN_VERB.search(norm):
            path = _raw_path(raw, norm)
            if path:
                return Plan("open_path", [("open_path", {"path": path})],
                            "The observation above says whether it actually "
                            "opened. Report exactly that. Never claim it is "
                            "on his screen unless the observation says it "
                            "opened.")
        expr = _calc_expression(raw)
        if expr:
            return Plan("calc", [("calc", {"expression": expr})],
                        "The exact result is in the observation above. "
                        "Give him the number. Do not recompute it "
                        "yourself.")
        fm = _FIND_ASK.search(raw)
        if fm:
            return Plan("find",
                        [("find", {"pattern": fm.group("p").strip()})],
                        "Matching files are in the observation above. "
                        "List the ones that matter and say where they are.")
        if _LIST_VERB.search(norm):
            path = _raw_path(raw, norm)
            if path:
                return Plan("list_dir", [("list_dir", {"path": path})],
                            "The directory listing is in the observation "
                            "above. Tell him what is there in a sentence or "
                            "two - do not read out every filename.")

    chat = _is_chat(norm)
    if chat:
        return Plan(chat, [], "This is small talk. Answer him directly in "
                             "a sentence. Do not use a tool.", chat=True)

    if _HYPOTHETICAL.search(norm) and not _POLITE_NOW.search(norm):
        return None

    # A keyword buried in a paragraph is a topic, not a command. "I was
    # thinking about the weather station I built last summer..." is not a
    # request for the forecast. Commands are short; reminiscence is not.
    # Rules with a hard anchor (an explicit URL, an explicit "search
    # for X") are exempt, because those are unambiguous at any length.
    words = norm.split()
    if len(words) > 14 and not _ANCHORED.search(norm):
        return None

    for name, pattern, build in _RULES:
        m = pattern.search(norm)
        if not m:
            continue
        try:
            built = build(m, norm)
        except Exception:
            return None
        if not built:
            continue
        prefetch, hint = built
        return Plan(name, list(prefetch), hint)
    return None


def describe(plan: Optional[Plan]) -> str:
    """One line for the log and the status strip."""
    if plan is None:
        return "no route"
    if plan.chat:
        return "%s (no tools)" % plan.name
    return "%s -> %s" % (plan.name,
                         ", ".join(t for t, _a in plan.prefetch) or "none")
