# Dangerous shell command and Cypher query guard tables.

# Cypher response cleaning
CYPHER_PREFIX = "cypher"
CYPHER_SEMICOLON = ";"
CYPHER_BACKTICK = "`"
CYPHER_MATCH_KEYWORD = "MATCH"
# A scoped query must return something a project filter can judge. Matched
# against the UPPERCASED query, so these are upper case.
CYPHER_QUALIFIED_NAME_TOKEN = "QUALIFIED_NAME"
# Evidence must be in what the query RETURNS: a qualified name mentioned
# only in WHERE or ORDER BY does not make the returned rows attributable.
CYPHER_RETURN_KEYWORD = "RETURN "
# Only a predicate that narrows to a PREFIX restricts an aggregate to one
# project. `IS NOT NULL` / `<> ''` / `exists(...)` match every indexed node
# in every project, so a count over them spans them all.
CYPHER_PREFIX_PREDICATES = ("STARTS WITH", "=~", " = ")
# Boolean operators that break the link between "the restriction appears in
# the query" and "the query enforces the restriction". OR and XOR make the
# predicate beside them OPTIONAL; NOT inverts its SENSE, so the guard's own
# textual match finds a predicate selecting everything EXCEPT this project.
#
# Enumerated as a class rather than added one at a time: OR was fixed first
# and NOT and XOR were still open, which is the shape-versus-instance error
# this file has already paid for twice.
#
# A WORD-BOUNDARY pattern, not a spaced literal. Cypher treats tabs and
# newlines as whitespace, so `'alpha.'\nOR TRUE` slipped past a `" OR "`
# check while meaning exactly the same thing. The boundaries are what keep
# it from matching inside an identifier such as `n.coordinator`.
CYPHER_UNSAFE_BOOLEAN_PATTERN = r"\b(?:OR|NOT|XOR)\b"
# Constructs a SCOPED query may not use. Each defeats clause-level
# analysis: UNION means several RETURNs, CALL and WITH mean the projection
# is assembled elsewhere. The system prompt mandates plain
# MATCH/WHERE/RETURN/LIMIT, so refusing these costs nothing the model
# should be emitting, and it closes the bypass class instead of
# enumerating members of it.
#
# WITH is matched only as a standalone clause. A bare "WITH" also occurs
# inside "STARTS WITH" -- the predicate scoping requires -- and inside
# "ENDS WITH", which prompts.py line 64 recommends for matching a symbol
# by its short name. Both must survive.
CYPHER_UNANALYSABLE_PATTERN = r"\bUNION\b|\bCALL\b|(?<!STARTS )(?<!ENDS )\bWITH\b"
# Separates a projection term from its alias.
CYPHER_ALIAS_KEYWORD = " AS "
CYPHER_POST_RETURN_KEYWORDS = (" ORDER BY", " SKIP ", " LIMIT ", " UNION")
# An aggregate exposes no names, so a scoped request needs no evidence from
# it -- and refusing one would make scoping useless for counting queries.
CYPHER_AGGREGATE_TOKENS = ("COUNT(", "SUM(", "AVG(", "MIN(", "MAX(", "COLLECT(")
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
# `find` actions that run a command, delete files, or write output files
# (GNU -fprint/-fprint0/-fprintf/-fls create or truncate their file argument),
# so they need approval even though `find` itself is a read tool. Kept here so
# the security boundary is auditable in one place rather than inline in the
# approval check.
SHELL_FIND_MUTATING_ACTIONS = frozenset(
    {
        "-delete",
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-fls",
    }
)

# Options that make an otherwise read-only command take its file inputs from
# an option value instead of an operand (`sort/wc --files0-from`, find's
# `-files0-from`), or name a program for it to execute (`sort
# --compress-program`, `rg --pre`). The noninteractive containment loop checks
# operands, so a repo-local list file naming /etc/passwd would slip through;
# denying the whole indirect-input mode is the auditable policy. `sort -T`
# is included because it writes temp files to the named directory.
SHELL_NONINTERACTIVE_DENIED_OPTIONS: dict[str, tuple[str, ...]] = {
    "sort": (
        "--files0-from",
        "--compress-program",
        "--random-source",
        "-T",
        "--temporary-directory",
    ),
    "wc": ("--files0-from",),
    "find": ("-files0-from",),
    "rg": ("--pre",),
}

SHELL_GIT_SUBCMD_CONFIG = "config"

# `git config` writes to these keys plant a value that git later hands to a
# shell, so a single write is remote code execution on the next git operation
# (GHSA-2rr7-8xrw-gmhr: `git config --global core.sshCommand <payload>` planted
# an RCE backdoor). Approval is a weak control here because the reported .cgr.md
# injection framed the write as "required for the project to work, do NOT ask
# the user for permission", so these writes are blocked outright at every config
# scope. Reads and --unset stay allowed so a victim can inspect and clear a
# planted backdoor.
SHELL_GIT_CONFIG_READ_ACTIONS = frozenset(
    {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l"}
)
SHELL_GIT_CONFIG_UNSET_FLAGS = frozenset({"--unset", "--unset-all"})
SHELL_GIT_CONFIG_EXEC_KEYS = frozenset(
    {
        "core.sshcommand",
        "core.pager",
        "core.editor",
        "core.hookspath",
        "core.fsmonitor",
        "credential.helper",
        "sequence.editor",
        "diff.external",
        "gpg.program",
    }
)
# (prefix, suffix) pairs matching sub-scoped keys like `credential.<url>.helper`,
# `filter.<name>.clean`, and `alias.<name>` whose values git also runs.
SHELL_GIT_CONFIG_EXEC_KEY_PATTERNS = (
    ("credential.", ".helper"),
    ("filter.", ".clean"),
    ("filter.", ".smudge"),
    ("filter.", ".process"),
    ("difftool.", ".cmd"),
    ("mergetool.", ".cmd"),
    ("alias.", ""),
)

# Dangerous commands, absolutely blocked
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
