# Dependency-file names and manifest parsing keys.

EXCLUDED_DEPENDENCY_NAMES = frozenset({"python", "php"})

DEPENDENCY_FILES = frozenset(
    {
        "pyproject.toml",
        "requirements.txt",
        "package.json",
        "cargo.toml",
        "go.mod",
        "gemfile",
        "composer.json",
        "pubspec.yaml",
    }
)
CSPROJ_SUFFIX = ".csproj"

DEP_KEY_TOOL = "tool"
DEP_KEY_POETRY = "poetry"
DEP_KEY_DEPENDENCIES = "dependencies"
DEP_KEY_DEV_DEPENDENCIES = "dev-dependencies"
DEP_KEY_PROJECT = "project"
DEP_KEY_OPTIONAL_DEPS = "optional-dependencies"
DEP_KEY_DEV_DEPS_JSON = "devDependencies"
DEP_KEY_PEER_DEPS = "peerDependencies"
DEP_KEY_REQUIRE = "require"
DEP_KEY_REQUIRE_DEV = "require-dev"
DEP_KEY_VERSION = "version"
DEP_KEY_GROUP = "group"

DEP_ATTR_INCLUDE = "Include"
DEP_ATTR_VERSION = "Version"
DEP_XML_PACKAGE_REF = "PackageReference"

DEP_EXCLUDE_PYTHON = "python"
DEP_EXCLUDE_PHP = "php"

DEP_FILE_PYPROJECT = "pyproject.toml"
DEP_FILE_REQUIREMENTS = "requirements.txt"
DEP_FILE_PACKAGE_JSON = "package.json"
DEP_FILE_CARGO = "cargo.toml"
DEP_FILE_GOMOD = "go.mod"
# The go.mod directive naming the module path that prefixes every import of
# the module's packages; a same-line comment (incl. `// Deprecated:`) may
# trail it.
GO_KEYWORD_MODULE = "module"
GO_KEYWORD_PACKAGE = "package"
GO_PACKAGE_MAIN = "main"
GO_MOD_COMMENT_PREFIX = "//"
DEP_FILE_GEMFILE = "gemfile"
DEP_FILE_COMPOSER = "composer.json"
DEP_FILE_PUBSPEC = "pubspec.yaml"

# pubspec.yaml dependency blocks; a `name: spec` under one of these top-level
# keys is a package, while a nested block (`flutter:\n    sdk: flutter`) has no
# inline version and is recorded name-only.
PUBSPEC_DEP_KEYS = frozenset({"dependencies", "dev_dependencies"})
PUBSPEC_COMMENT_PREFIX = "#"
PUBSPEC_KEY_SEP = ":"

GOMOD_REQUIRE_BLOCK_START = "require ("
GOMOD_BLOCK_END = ")"
GOMOD_REQUIRE_LINE_PREFIX = "require "
GOMOD_COMMENT_PREFIX = "//"

GEMFILE_GEM_PREFIX = "gem "

IMPORT_CACHE_TTL = 3600
IMPORT_CACHE_DIR = ".cache/codebase_rag"
IMPORT_CACHE_FILE = "stdlib_cache.json"
IMPORT_CACHE_KEY = "cache"
IMPORT_TIMESTAMPS_KEY = "timestamps"
