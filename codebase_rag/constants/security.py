# Dangerous shell command and Cypher query guard tables.

# Cypher response cleaning
CYPHER_PREFIX = "cypher"
CYPHER_SEMICOLON = ";"
CYPHER_BACKTICK = "`"
CYPHER_MATCH_KEYWORD = "MATCH"
CYPHER_DANGEROUS_KEYWORDS: frozenset[str] = frozenset(
    {
        "DELETE",
        "DETACH",
        "DROP",
        "CREATE INDEX",
        "CREATE CONSTRAINT",
        "REMOVE",
        "SET",
        "MERGE",
        "CREATE",
        "LOAD CSV",
        "FOREACH",
    }
)

CYPHER_ALLOWED_PROCEDURE_PREFIXES: frozenset[str] = frozenset(
    {
        "algo.",
        "betweenness_centrality.",
        "biconnected_components.",
        "bridges.",
        "community_detection.",
        "cycles.",
        "degree_centrality.",
        "graph_analyzer.",
        "graph_util.",
        "igraphalg.",
        "katz_centrality.",
        "leiden_community_detection.",
        "neighbors.",
        "node_similarity.",
        "nxalg.",
        "pagerank.",
        "path.",
        "schema.",
        "weakly_connected_components.",
        "wcc.",
    }
)

# Shell command constants
SHELL_CMD_GREP = "grep"
SHELL_CMD_GIT = "git"
SHELL_CMD_RM = "rm"
SHELL_RM_RF_FLAG = "-rf"
SHELL_RETURN_CODE_ERROR = -1
SHELL_PIPE_OPERATORS = ("|", "&&", "||", ";")
SHELL_SUBSHELL_PATTERNS = ("$(", "`")
SHELL_REDIRECT_OPERATORS = frozenset({">", ">>", "<", "<<"})

# Dangerous commands - absolutely blocked
SHELL_DANGEROUS_COMMANDS = frozenset(
    {
        "dd",
        "mkfs",
        "mkfs.ext4",
        "mkfs.ext3",
        "mkfs.xfs",
        "mkfs.btrfs",
        "mkfs.vfat",
        "fdisk",
        "parted",
        "shred",
        "wipefs",
        "mkswap",
        "swapon",
        "swapoff",
        "mount",
        "umount",
        "insmod",
        "rmmod",
        "modprobe",
        "shutdown",
        "reboot",
        "halt",
        "poweroff",
        "init",
        "telinit",
        "systemctl",
        "service",
        "chroot",
        "nohup",
        "disown",
        "crontab",
        "at",
        "batch",
    }
)

# Dangerous rm flags
SHELL_RM_DANGEROUS_FLAGS = frozenset({"-rf", "-fr"})
SHELL_RM_FORCE_FLAG = "-f"

# System directories to protect from rm -rf
SHELL_SYSTEM_DIRECTORIES = frozenset(
    {
        "bin",
        "boot",
        "dev",
        "etc",
        "home",
        "lib",
        "lib64",
        "media",
        "mnt",
        "opt",
        "proc",
        "root",
        "run",
        "sbin",
        "srv",
        "sys",
        "tmp",
        "usr",
        "var",
    }
)

# Dangerous patterns for full pipeline (cross-segment patterns with pipes/operators)
SHELL_DANGEROUS_PATTERNS_PIPELINE = (
    (r"(wget|curl)\s+.*\|\s*(sh|bash|zsh|ksh)", "remote script execution"),
    (r"(wget|curl)\s+.*>\s*.*\.sh\s*&&", "download and execute script"),
    (r"base64\s+-d.*\|", "base64 decode pipe execution"),
)

# Build system directory regex pattern dynamically
_SYSTEM_DIRS_PATTERN = "|".join(SHELL_SYSTEM_DIRECTORIES)

# Dangerous patterns for individual segments (per-command patterns)
SHELL_DANGEROUS_PATTERNS_SEGMENT = (
    (r"rm\s+.*-[rf]+\s+/($|\s)", "rm with root path"),
    (rf"rm\s+.*-[rf]+\s+/({_SYSTEM_DIRS_PATTERN})($|/|\s)", "rm with system directory"),
    (r"rm\s+.*-[rf]+\s+~($|\s)", "rm with home directory"),
    (r"rm\s+.*-[rf]+\s+\*", "rm with wildcard"),
    (r"rm\s+.*-[rf]+\s+\.\.", "rm with parent directory"),
    (r"dd\s+.*of=/dev/", "dd writing to device"),
    (r">\s*/dev/sd[a-z]", "redirect to disk device"),
    (r">\s*/dev/nvme", "redirect to nvme device"),
    (r">\s*/dev/null.*<", "null device manipulation"),
    (r"chmod\s+.*-R\s+777\s+/", "recursive 777 on root"),
    (r"chmod\s+.*777\s+/($|\s)", "777 on root"),
    (r"chown\s+.*-R\s+.*\s+/($|\s)", "recursive chown on root"),
    (r":\(\)\s*\{.*:\s*\|", "fork bomb pattern"),
    (r"mv\s+.*\s+/dev/null", "move to /dev/null"),
    (r"ln\s+-[sf]+\s+/dev/null", "symlink to /dev/null"),
    (r"cat\s+.*/dev/zero", "cat /dev/zero"),
    (r"cat\s+.*/dev/random", "cat /dev/random"),
    (r">\s*/etc/passwd", "overwrite passwd"),
    (r">\s*/etc/shadow", "overwrite shadow"),
    (r">\s*/etc/sudoers", "overwrite sudoers"),
    (r"echo\s+.*>\s*/etc/", "write to /etc"),
    (
        r"python.*-c.*(import\s+os|__import__\s*\(\s*['\"]os['\"]\s*\))",
        "python os import in command",
    ),
    (r"perl\s+-e", "perl one-liner"),
    (r"ruby\s+-e", "ruby one-liner"),
    (r"nc\s+-[el]", "netcat listener"),
    (r"ncat\s+-[el]", "ncat listener"),
    (r"/dev/tcp/", "bash tcp device"),
    (r"eval\s+", "eval command"),
    (r"exec\s+[0-9]+<>", "exec file descriptor manipulation"),
    (r"awk\s+.*system\s*\(", "awk system() call"),
    (r"awk\s+.*getline\s*[<|]", "awk getline file/pipe execution"),
    (r"sed\s+.*s(.).*?\1.*?\1[gip]*e[gip]*", "sed execute flag"),
    (r"xargs\s+.*(rm|chmod|chown|mv|dd|mkfs)", "xargs with destructive command"),
    (r"xargs\s+-I.*sh", "xargs shell execution"),
    (r"xargs\s+.*bash", "xargs bash execution"),
)
