import itertools, random, sys, json, string
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

PREFIX = ["", "sudo ", "sudo -u root ", "doas ", "env FOO=1 ", "nice -n 5 ",
          "ionice -c 3 ", "timeout 5 ", "timeout 1.5 ", "setsid ", "nohup ",
          "stdbuf -o0 ", "sh -c '", "bash -c \"", "sudo sh -c '",
          "echo $(", "`", "ls && ", "ls; ", "true || "]
SUFFIX = {"sh -c '": "'", "bash -c \"": '"', "sudo sh -c '": "'",
          "echo $(": ")", "`": "`"}
BAD = ["rm -rf /", "rm -rf /etc", "rm -rf ~", "rm -rf $HOME",
       "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/nvme0n1",
       "chmod -R 000 /", "shred /dev/sda", "poweroff", "wipefs -a /dev/sdb"]
GOOD = ["ls -la", "cat README.md", "rm -rf ./node_modules", "git pull",
        "grep -r todo src/", "df -h", "pacman -Syu neovim", "uptime",
        "cp a b", "mv old.txt new.txt", "chmod 644 file.txt",
        "rm -rf ~/.cache/thumbnails", "mkdir -p ~/dev/x", "free -h"]

missed = fp = crashes = total = 0
for pre, cmd in itertools.product(PREFIX, BAD):
    s = pre + cmd + SUFFIX.get(pre, "")
    total += 1
    try:
        if not G.is_destructive_command(s):
            missed += 1; print("MISS:", s)
    except Exception as e:
        crashes += 1; print("CRASH:", s, e)
for pre, cmd in itertools.product(PREFIX, GOOD):
    s = pre + cmd + SUFFIX.get(pre, "")
    total += 1
    try:
        if G.is_destructive_command(s):
            fp += 1; print("FP:", s)
    except Exception as e:
        crashes += 1; print("CRASH:", s, e)

# random junk must never crash either gate or the parser
random.seed(7)
alphabet = string.printable
for _ in range(4000):
    s = "".join(random.choice(alphabet) for _ in range(random.randint(1, 90)))
    try:
        G.is_destructive_command(s)
        G.command_needs_confirmation(s, G.DEFAULTS)
        G.is_network_pipe_to_shell(s)
        G.extract_actions(s)
        G.strip_action_json(s)
        G.html_to_text(s)
        G.clean_for_speech(s)
    except Exception as e:
        crashes += 1; print("FUZZ CRASH:", repr(s[:60]), type(e).__name__, e)

print("combos=%d missed=%d falsepos=%d crashes=%d (+4000 fuzz strings)"
      % (total, missed, fp, crashes))
sys.exit(1 if (missed or crashes) else 0)
