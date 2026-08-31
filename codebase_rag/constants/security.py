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
# Named so the alias-binding check can exclude it: a regex operand cannot be
# shown limited to one project by inspection, whereas a literal can.
CYPHER_REGEX_PREDICATE = "=~"
# Stripped from a captured aggregate operand in code rather than matched in
# the pattern: matching it there placed two whitespace-consuming quantifiers
# side by side, which backtracks super-linearly.
CYPHER_DISTINCT_KEYWORD = "DISTINCT"
# Cypher literals that uppercase into something indistinguishable from an
# alias, so an aggregate over one would look bindable. Enumerating is sound
# here because the LANGUAGE defines exactly these three -- unlike a list of
# "constant spellings", which is open-ended and missed a quoted string.
CYPHER_LITERAL_OPERANDS = frozenset({"TRUE", "FALSE", "NULL"})
# The WHERE clause a scoped aggregate is allowed to have: a conjunction of
# plain `<entity>.<property> <op> <value>` comparisons, joined by AND.
#
# A WHITELIST, after a blacklist of `OR|NOT|XOR` proved to be the wrong
# shape. Those three widen or invert a predicate, but Cypher's boolean
# surface is larger -- `CASE WHEN <pred> THEN true ELSE true END` and
# `coalesce(<pred>, true)` contain none of them, evaluate true for every
# row, and were accepted. A blacklist tests for known-bad SPELLINGS; the
# contract is that the restriction BINDS, which is a property.
#
# Sound only because the input language is small: the Cypher is generated
# against a system prompt mandating plain MATCH/WHERE/RETURN/LIMIT, and
# anything outside that shape is refused rather than analysed -- the same
# default-deny `CYPHER_UNANALYSABLE_PATTERN` applies to query structure.
# If this ever accepts user-authored Cypher, this rule is what must be
# revisited first (issue #1494).
CYPHER_CONJUNCT_PATTERN = (
    r"^\s*[A-Z_][A-Z0-9_]*\.[A-Z_][A-Z0-9_]*\s*"
    r"(?:STARTS WITH|ENDS WITH|CONTAINS|=~|<>|=|<|>|<=|>=)\s*"
    r"(?:'[^']*'|\"[^\"]*\"|[A-Z0-9_.$]+)\s*$"
)
CYPHER_CONJUNCTION_SEPARATOR = r"\bAND\b"
CYPHER_WHERE_KEYWORD = "WHERE"
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
# git flags that set a config key inline for a single command. `--config-env`
# reads the value from an environment variable but sets the same keys, so it
# reaches the same executable keys as `-c` (GHSA-wvxg-744g-6pcg).
# git's own options, before the subcommand, that take a SEPARATE value. The
# scan must step over the value: mistaking it for the subcommand stops the
# scan early and misses a `-c` that follows (`git -C /tmp -c <key>=<v> ...`).
SHELL_GIT_GLOBAL_VALUE_FLAGS = frozenset(
    {
        "-C",
        "-c",
        "--config-env",
        "--git-dir",
        "--work-tree",
        "--namespace",
        "--super-prefix",
    }
)

# git globals whose argument is OPTIONAL and attached-only: bare `--exec-path`
# prints the path and exits rather than consuming the next token, so treating
# it as value-taking makes the scan swallow a following `-c` and miss the key
# behind it. Verified against git itself: these four print and exit, while
# -C/--git-dir/--work-tree/--namespace all error with "no ... given".
# Same class as the GNU xargs -i/-l/-e misfiling.
SHELL_GIT_OPTIONAL_ARG_FLAGS = frozenset(
    {
        "--exec-path",
        "--html-path",
        "--man-path",
        "--info-path",
    }
)

SHELL_GIT_INLINE_CONFIG_FLAGS = frozenset({"-c", "--config-env"})

SHELL_CMD_XARGS = "xargs"
SHELL_CMD_FIND = "find"

# Allowlisted commands that are general-purpose program launchers: each can be
# steered into running a program the allowlist never vetted. Under `--yolo` the
# allowlist is bypassed wholesale, so these are blocked outright there rather
# than merely gated behind an approval nobody is present to give
# (GHSA-wvxg-744g-6pcg).
# Depth cap for launcher-nesting recursion. Each level consumes at least one
# token, so real commands never approach it; the cap converts a pathological
# input from a runtime RecursionError into a validator refusal.
SHELL_MAX_LAUNCHER_NESTING = 16

SHELL_CMD_AWK = "awk"

# Constructs through which an awk PROGRAM runs a command or writes a file:
# system(), a pipe redirection (`print x | cmd`, gawk's `|&`), getline from a
# command, close() on a command stream, and `>`/`>>` to a named file. Matching
# the surrounding spelling instead was defeated by every indirection tried --
# a variable holding the command, -v assignment, string concatenation, a
# program read with -f, or a newline in the program text. These tokens cannot
# be avoided: awk has no other way to reach a subprocess, so detecting them in
# the program argument bans the capability rather than the spellings.
# awk flags taking a separate value: the token after them is that value, not
# the program. Skipping the flag but not its value made `awk -v c=id '...'`
# treat `c=id` as the program text and never scan the real one -- the
# optional-vs-required argument confusion again, fourth occurrence.
SHELL_AWK_VALUE_FLAGS = frozenset({"-v", "-F", "-f"})

SHELL_CMD_SED = "sed"

# sed constructs that run a command or write a file. GNU sed executes via the
# `s///e` flag AND a standalone `e` command; both `w FILE` and `s///w FILE`
# write a named file. This host runs BSD sed, which rejects `e`, but CI runs
# GNU -- the policy must deny regardless of which binary is present, or the
# gate passes locally and fails open in CI.
SHELL_SED_EXEC_TOKENS = (
    # sed commands that reach a subprocess or a file. Anchored on the command
    # LETTER, which the attacker cannot avoid, rather than on the address that
    # precedes it -- addresses take many forms (`0~3`, `/re/I`, `\%re%`,
    # `1,+2`, `!`, a leading newline) and enumerating them let seven spellings
    # through, exactly as enumerating awk's spellings did.
    #
    # A command letter is preceded by a command separator (start, `;`, `{`,
    # newline) or by an address, which always ends in a digit, `/`, `%`, `+`,
    # `!`, `~`, `,` or a closing delimiter. So: any of those, then the letter.
    (r"(?:^|[;{\n]|[\d/%+!~,I$])\s*e(?:\s|$)", "e command"),
    (r"s(.).*?\1.*?\1[gip0-9]*e", "s///e execute flag"),
    (r"(?:^|[;{\n]|[\d/%+!~,I$])\s*[wWrR]\s+\S", "reads or writes a named file"),
    (r"s(.).*?\1.*?\1[gip0-9]*w\s+\S", "s///w writes a named file"),
)

# sed flags naming a script file this validator cannot read.
SHELL_SED_SCRIPT_FILE_FLAGS = frozenset({"-f", "--file"})

SHELL_AWK_EXEC_TOKENS = (
    (r"system\s*\(", "system() call"),
    (r"\|&", "coprocess pipe"),
    (r"\|", "pipe to or from a command"),
    (r"\bgetline\b", "getline"),
    (r"close\s*\(", "close() on a command stream"),
    # The target need not be quoted -- `print 1 > f` with f a variable wrote
    # outside the root. awk's output redirect always follows a print/printf
    # output list, which is what distinguishes it from a `>` comparison
    # (`NR>1`), so match the construct rather than the target's spelling.
    (r"\b(print|printf)\b[^;}]*>>?\s*\S", "redirect to a file"),
)

SHELL_LAUNCHER_COMMANDS = frozenset({"xargs", "uv", "pytest", "pre-commit", "find"})


# `xargs` flags that take a separate value argument; the value is not the
# command xargs will launch, so the scan must step over both (GHSA-wvxg-744g-6pcg).
SHELL_XARGS_VALUE_FLAGS = frozenset(
    {
        "-I",
        "-L",
        "-n",
        "-P",
        "-s",
        "-d",
        "-E",
        "-a",
        # BSD/macOS xargs: -J replace-string, -R replace-count, -S replsize,
        # -o is a bare toggle but listed under boolean flags below.
        "-J",
        "-R",
        "-S",
        "--max-args",
        "--max-procs",
        "--max-chars",
        "--delimiter",
        "--arg-file",
        "--process-slot-var",
    }
)

# `xargs` flags that take no value. The scanner must distinguish these from
# value-taking flags to know whether the next token is a value or the program;
# any flag in NEITHER set is unknown, and the scan fails closed rather than
# guessing (GHSA-wvxg-744g-6pcg).
# GNU xargs declares -i/-l/-e with getopt's double colon (optional argument),
# which accepts a value only when ATTACHED: `xargs -i python3 cat` parses as
# -i with no value and python3 as the utility. Treating them as separate-value
# flags makes the scan swallow the program token and name its argument
# instead, which is the `-J cat python3` bypass again in lowercase. BSD xargs
# has no -i/-l/-e at all, so attached-only is correct on both implementations.
# The uppercase -I/-L/-E take a REQUIRED argument and stay in the value set.
SHELL_XARGS_OPTIONAL_ARG_FLAGS = frozenset(
    {
        "-i",
        "-l",
        "-e",
        "--replace",
        "--max-lines",
        "--eof",
    }
)

SHELL_XARGS_BOOLEAN_FLAGS = frozenset(
    {
        "-0",
        "-o",
        "-p",
        "-r",
        "-t",
        "-x",
        "--null",
        "--open-tty",
        "--interactive",
        "--no-run-if-empty",
        "--verbose",
        "--exit",
    }
)

# Sentinel: the scan could not determine what `xargs` would launch, so the
# caller must block rather than fall through to a possibly-wrong answer.
SHELL_XARGS_UNKNOWN_LAUNCH = "<unknown>"

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

# git subcommands that run a caller-supplied command. `filter-branch
# --tree-filter 'cmd'` was verified executing in a scratch repo; `bisect run`
# and `submodule foreach` are documented executors that need a bisect in
# progress or a submodule present to demonstrate. They reach a subprocess
# without ever touching the shell allowlist, so git needs its own check --
# `git` is allowlisted and only its `-c`/`config` exec keys were guarded.
SHELL_GIT_EXEC_SUBCOMMANDS = frozenset(
    {
        "filter-branch",
        "bisect",
        "submodule",
    }
)

# git FLAGS that take a command and run it, whatever the subcommand. Verified
# executing locally in a scratch repo: `rebase --exec` ran twice, `difftool
# --extcmd` once. The upload-pack/receive-pack family names a program run on
# the REMOTE, so it is not local execution, but it is still a caller-chosen
# program name reaching a git invocation and is refused on the same ground.
SHELL_GIT_EXEC_FLAGS = frozenset(
    {
        "--exec",
        "--extcmd",
        "--tool",
        "--upload-pack",
        "--receive-pack",
        "--smtp-server",
        "--gpg-sign",
    }
)

# The argument that turns one of those subcommands into a command runner.
# `git submodule status` and `git bisect start` launch nothing.
SHELL_GIT_EXEC_SUBCOMMAND_ARGS = frozenset(
    {
        "foreach",
        "run",
        "--tree-filter",
        "--index-filter",
        "--parent-filter",
        "--msg-filter",
        "--commit-filter",
        "--tag-name-filter",
        "--subdirectory-filter",
        "--env-filter",
    }
)

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
        # Program-valued keys git hands to a shell. core.gitProxy and
        # protocol.<name>.command are documented executors; ssh.variant and
        # init.templateDir steer which program runs or where hooks come from.
        "core.gitproxy",
        "uploadpack.packobjectshook",
        "ssh.variant",
        "init.templatedir",
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
    # Verified executing in a scratch repo: `diff.<driver>.textconv` ran on a
    # `git diff` with a matching .gitattributes, and `trailer.<token>.command`
    # ran on `interpret-trailers`. The rest are documented program-valued keys
    # of the same family, added on semantics rather than one probe each.
    ("diff.", ".textconv"),
    ("diff.", ".command"),
    ("merge.", ".driver"),
    ("trailer.", ".command"),
    ("pager.", ""),
    ("protocol.", ".command"),
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
    # awk also runs a command by piping to it (`print x | "cmd"`) and by
    # reading one (`"cmd" | getline`). Verified executing on this platform:
    # the system() pattern alone left `awk 'BEGIN{print 1 | "echo X"}'`
    # running in both modes. `close()` and `printf ... |` are the same door.
    (r"awk\s+.*getline\s*[<|]", "awk getline file/pipe execution"),
    (r"sed\s+.*s(.).*?\1.*?\1[gip]*e[gip]*", "sed execute flag"),
    (r"xargs\s+.*(rm|chmod|chown|mv|dd|mkfs)", "xargs with destructive command"),
    (r"xargs\s+-I.*sh", "xargs shell execution"),
    (r"xargs\s+.*bash", "xargs bash execution"),
)
