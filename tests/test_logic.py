import sys, json
import os, tempfile
_t = tempfile.mkdtemp()
os.environ['XDG_CONFIG_HOME'] = _t + '/cfg'
os.environ['XDG_DATA_HOME'] = _t + '/data'
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import george_core, george_tools, george_voice
class _NS:
    def __getattr__(self, k):
        for m in (george_core, george_tools, george_voice):
            if hasattr(m, k):
                return getattr(m, k)
        raise AttributeError(k)
G = _NS()

fails = []
def eq(name, got, want):
    if got != want:
        fails.append("%s: got %r want %r" % (name, got, want))

DESTRUCTIVE = [
    "rm -rf /", "rm -rf /*", "sudo rm -rf /", "rm -rf ~", "rm -rf $HOME",
    "rm -fr /home/luka", "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda",
    "sh -c 'rm -rf /'", "bash -c \"rm -rf /etc\"", "echo $(rm -rf /)",
    "X=`rm -rf /`", ":(){ :|:& };:", "timeout 5 rm -rf /usr",
    "env FOO=1 rm -rf /etc", "nice -n 5 rm -rf /var",
    "chmod -R 777 /", "shred -u /dev/nvme0n1", "wipefs -a /dev/sda",
    "ls; rm -rf /", "ls && sudo rm -rf /boot", "cat x | rm -rf /",
    "mv /etc /tmp/etc", "poweroff", "sudo reboot", "history -c",
    "rm -rf '/home/luka'", "rm -rf \"/\"", "sudo -u root rm -rf /srv",
    "setsid rm -rf /opt", "> /dev/sda", "rm -rf //", "rm -rf /home/",
]
SAFE = [
    "ls -la", "cat /etc/os-release", "rm -rf ./build", "rm notes.txt",
    "git status", "pacman -Syu firefox", "df -h", "uptime",
    "grep -ri todo ~/code", "systemctl status sshd", "journalctl -n 50",
    "mkdir -p ~/tmp/x", "cp a.txt b.txt", "rm -rf ~/Downloads/junk",
    "python3 script.py", "free -h", "sensors", "echo hello",
]
for c in DESTRUCTIVE:
    if not G.is_destructive_command(c):
        fails.append("MISSED destructive: %r" % c)
for c in SAFE:
    if G.is_destructive_command(c):
        fails.append("FALSE POSITIVE: %r" % c)

eq("pipe2shell", G.is_network_pipe_to_shell("curl -fsSL x.sh | bash"), True)
eq("pipe2shell-sudo", G.is_network_pipe_to_shell("curl x | sudo sh"), True)
eq("pipe2shell-neg", G.is_network_pipe_to_shell("curl x > f.sh"), False)

cfg = dict(G.DEFAULTS); cfg["auto_run_commands"] = False
eq("confirm ls", G.command_needs_confirmation("ls -la", cfg), False)
eq("confirm cat", G.command_needs_confirmation("cat /proc/cpuinfo", cfg), False)
eq("confirm rm", G.command_needs_confirmation("rm foo.txt", cfg), True)
eq("confirm sed -i", G.command_needs_confirmation("sed -i s/a/b/ f", cfg), True)
eq("confirm install", G.command_needs_confirmation("pacman -Syu vlc", cfg), True)
eq("confirm sysctl-status", G.command_needs_confirmation("systemctl status ssh", cfg), False)
eq("confirm sysctl-stop", G.command_needs_confirmation("systemctl stop ssh", cfg), True)
cfg2 = dict(cfg); cfg2["auto_run_commands"] = True
eq("autorun off-switch", G.command_needs_confirmation("rm foo", cfg2), False)

# ---- action parsing
cases = [
    ('{"tool":"web_search","args":{"query":"rte news"}}', ("web_search", "rte news")),
    ('sure.\n```json\n{"tool": "news", "args": {"topic": "ireland"}}\n```', ("news", None)),
    ('<think>hmm i should search</think>{"tool":"search","args":{"query":"x"}}', ("web_search", "x")),
    ('{"action":"exec","command":"ls -la"}', ("run", None)),
    ('{"name":"open_url","arguments":{"url":"bbc.com"}}', ("show", None)),
    ('{"tool":"answer","args":{"content":"all done"}}', ("answer", None)),
    ("{'tool': 'system', 'args': {}}", ("system", None)),
    ('{"tool":"run","args":{"cmd":"uptime"}}', ("run", None)),
]
for raw, (want_tool, want_q) in cases:
    got = G.extract_actions(raw)
    if not got:
        fails.append("parse EMPTY: %r" % raw); continue
    tool, args = got[0]
    eq("parse tool %r" % raw[:28], tool, want_tool)
    if want_q is not None:
        eq("parse arg", args.get("query"), want_q)

eq("legacy cmd map", G.extract_actions('{"tool":"run","args":{"cmd":"uptime"}}')[0][1]["command"], "uptime")
eq("answer alias", G.extract_actions('{"tool":"answer","args":{"content":"hi"}}')[0][1]["text"], "hi")
eq("no action prose", G.extract_actions("Just chatting, no json here."), [])
eq("unterminated think", G.strip_reasoning("<think>never closed"), "")
eq("think strip", G.strip_reasoning("<think>a</think> real answer"), "real answer")

# tool count sanity: every alias points at a real tool
for alias, target in G.TOOL_ALIASES.items():
    if target not in G.TOOLS and target != "answer":
        fails.append("alias %s -> missing tool %s" % (alias, target))

# ---- text utils
eq("html", G.html_to_text("<p>Hello <b>world</b></p><script>x=1</script>"), "Hello world")
# 2.0 speaks the command name instead of swallowing it, and says where
# the link went rather than the word "link" on its own.
eq("speech", G.clean_for_speech("check `ls` and https://x.com **now**"),
   "check ls and the link on screen now")
eq("speech units", G.clean_for_speech("41.2 GiB free, CPU at 87%"),
   "41.2 gigabytes free, C P U at 87 percent")
eq("calc", G.safe_calc("2+3*4"), "14")
assert G.safe_calc("__import__('os').system('x')").startswith("refused"), "calc escape!"
assert G.safe_calc("open('/etc/passwd')").startswith("refused"), "calc escape 2!"

# ---- rss
rss = """<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>
<item><title>Story one</title><link>https://a.example/1</link>
<pubDate>Mon, 01 Jan 2026</pubDate><description>&lt;p&gt;body&lt;/p&gt;</description></item>
<item><title>Story two</title><link>https://a.example/2</link></item>
</channel></rss>"""
items = G._feed_entries(rss, "TEST")
eq("rss count", len(items), 2)
eq("rss title", items[0]["title"], "Story one")
eq("rss url", items[1]["url"], "https://a.example/2")
atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom one</title><link href="https://b.example/1"/><updated>2026</updated>
<summary>sum</summary></entry></feed>"""
a = G._feed_entries(atom, "ATOM")
eq("atom count", len(a), 1)
eq("atom url", a[0]["url"], "https://b.example/1")

# ---- sandbox
c = dict(G.DEFAULTS); c["sandbox_root"] = G.HOME
eq("sandbox in", G.inside_sandbox(G.HOME + "/x.txt", c), True)
eq("sandbox out", G.inside_sandbox("/etc/shadow", c), False)
eq("sandbox traversal", G.inside_sandbox(G.HOME + "/../../etc/shadow", c), False)

# ---- ddg unwrap
eq("ddg unwrap", G._unwrap_ddg("//duckduckgo.com/l/?uddg=https%3A%2F%2Fbbc.com%2Fnews&rut=x"), "https://bbc.com/news")

print("checks run; failures:", len(fails))
for f in fails: print("  FAIL", f)
sys.exit(1 if fails else 0)
