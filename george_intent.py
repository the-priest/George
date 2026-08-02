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
    """
    s = str(text or "").lower()
    s = s.replace("\u2019", "'")                 # smart apostrophe
    s = _CONTRACTIONS.sub(lambda m: m.group(1) + "s", s)
    s = s.replace("n't", "nt").replace("'re", "re").replace("'ll", "ll")
    s = _WS.sub(" ", _PUNCT.sub(" ", s)).strip()
    return s


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
    r"what can you do|what do you do|help)\b")


def _is_chat(norm: str) -> Optional[str]:
    if not norm:
        return None
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
    where = (m.group("where") or "").strip() if "where" in (
        m.groupdict() or {}) else ""
    for lead in ("in ", "for ", "at ", "near "):
        if where.startswith(lead):
            where = where[len(lead):]
    return ([("weather", {"location": where})],
            "The current conditions are in the observation above. Give "
            "him the answer in a sentence or two - what it is doing and "
            "whether he needs a coat. Do not call the weather tool again.")


_RULES: List[Tuple[str, "re.Pattern", Any]] = [

    # ---- open a URL on screen -----------------------------------------
    # FIRST on purpose. A URL can contain any keyword -- "open
    # https://news.ycombinator.com" matched the news rule and pulled RSS
    # feeds instead of opening the page he named.
    ("show", re.compile(
        r"\b(open|show|pull up|bring up|put)\b.{0,40}?"
        r"(?P<url>(https?://|www\.)[^\s]+)"),
     lambda m, n: ([("show", {"url": m.group("url")})],
                   "The observation above says whether it actually "
                   "opened. Report exactly that - if it failed, say it "
                   "failed and why. Never claim it is on his screen "
                   "unless the observation says it opened.")),

    # ---- weather -----------------------------------------------------
    ("weather", re.compile(
        r"\b(weather|forecast|temperature|how (hot|cold|warm)|"
        r"(is|will) it (rain|snow)\w*|do i need (a|an) (coat|jacket|"
        r"umbrella))\b(?P<where>.*)?"), _weather_args),

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
    ("pkg", re.compile(
        r"\b(is (?P<p1>[\w.+-]{2,40}) installed|"
        r"do i have (?P<p2>[\w.+-]{2,40}) installed|"
        r"(install|search for|look for) (the )?(package )?(?P<p3>[\w.+-]{2,40}))\b"),
     lambda m, n: ([("pkg", {
         # READ-ONLY ONLY. The router runs this BEFORE the model has
         # decided anything, so it must never take an action that
         # changes the machine. "install ripgrep" prefetches a SEARCH;
         # the model then proposes the install and he confirms it, which
         # is the path every state change has to take.
         "action": "installed" if "installed" in n else "search",
         "package": (m.group("p1") or m.group("p2") or m.group("p3") or "")})],
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
    ("clock", re.compile(
        r"^(what(s| is)? (the )?(time|date)|what day is it|"
        r"time please|whats today)\b"),
     lambda m, n: ([],
                   "The current date and time are in your CONTEXT block. "
                   "Answer directly - no tool is needed.")),

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
        r"what have you remembered|your memory|what do you recall)\b"),
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
