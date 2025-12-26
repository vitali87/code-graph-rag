from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from loguru import logger

from ..constants import SEPARATOR_DOT, SupportedLanguage
from ..types_defs import ASTNode, SimpleNameLookup
from .class_ingest import ClassIngestMixin
from .dependency_parser import parse_dependencies
from .function_ingest import FunctionIngestMixin
from .handlers import get_handler
from .js_ts.ingest import JsTsIngestMixin
from .utils import safe_decode_with_fallback

if TYPE_CHECKING:
    from ..services import IngestorProtocol
    from ..types_defs import LanguageQueries
    from .handlers import LanguageHandler
    from .import_processor import ImportProcessor


class DefinitionProcessor(
    FunctionIngestMixin,
    ClassIngestMixin,
    JsTsIngestMixin,
):
    _handler: LanguageHandler

    def __init__(
        self,
        ingestor: IngestorProtocol,
        repo_path: Path,
        project_name: str,
        function_registry: Any,
        simple_name_lookup: SimpleNameLookup,
        import_processor: ImportProcessor,
        module_qn_to_file_path: dict[str, Path],
    ):
        super().__init__()
        self.ingestor = ingestor
        self.repo_path = repo_path
        self.project_name = project_name
        self.function_registry = function_registry
        self.simple_name_lookup = simple_name_lookup
        self.import_processor = import_processor
        self.module_qn_to_file_path = module_qn_to_file_path
        self.class_inheritance: dict[str, list[str]] = {}
        self._handler = get_handler(SupportedLanguage.PYTHON)

    def process_file(
        self,
        file_path: Path,
        language: SupportedLanguage,
        queries: dict[SupportedLanguage, LanguageQueries],
        structural_elements: dict[Path, str | None],
    ) -> tuple[ASTNode, SupportedLanguage] | None:
        if isinstance(file_path, str):
            file_path = Path(file_path)
        relative_path = file_path.relative_to(self.repo_path)
        relative_path_str = str(relative_path)
        logger.info(f"Parsing and Caching AST for {language}: {relative_path_str}")

        try:
            if language not in queries:
                logger.warning(f"Unsupported language '{language}' for {file_path}")
                return None

            self._handler = get_handler(language)
            source_bytes = file_path.read_bytes()
            lang_queries = queries[language]
            parser = lang_queries.get("parser")
            if not parser:
                logger.warning(f"No parser available for {language}")
                return None

            tree = parser.parse(source_bytes)
            root_node = tree.root_node

            module_qn = SEPARATOR_DOT.join(
                [self.project_name] + list(relative_path.with_suffix("").parts)
            )
            if file_path.name in ["__init__.py", "mod.rs"]:
                module_qn = SEPARATOR_DOT.join(
                    [self.project_name] + list(relative_path.parent.parts)
                )
            self.module_qn_to_file_path[module_qn] = file_path

            self.ingestor.ensure_node_batch(
                "Module",
                {
                    "qualified_name": module_qn,
                    "name": file_path.name,
                    "path": relative_path_str,
                },
            )

            parent_rel_path = relative_path.parent
            parent_container_qn = structural_elements.get(parent_rel_path)
            parent_label, parent_key, parent_val = (
                ("Package", "qualified_name", parent_container_qn)
                if parent_container_qn
                else (
                    ("Folder", "path", str(parent_rel_path))
                    if parent_rel_path != Path(".")
                    else ("Project", "name", self.project_name)
                )
            )
            self.ingestor.ensure_relationship_batch(
                (parent_label, parent_key, parent_val),
                "CONTAINS_MODULE",
                ("Module", "qualified_name", module_qn),
            )

            self.import_processor.parse_imports(root_node, module_qn, language, queries)
            self._ingest_missing_import_patterns(
                root_node, module_qn, language, queries
            )
            if language == SupportedLanguage.CPP:
                self._ingest_cpp_module_declarations(root_node, module_qn, file_path)
            self._ingest_all_functions(root_node, module_qn, language, queries)
            self._ingest_classes_and_methods(root_node, module_qn, language, queries)
            self._ingest_object_literal_methods(root_node, module_qn, language, queries)
            self._ingest_commonjs_exports(root_node, module_qn, language, queries)
            self._ingest_es6_exports(root_node, module_qn, language, queries)
            self._ingest_assignment_arrow_functions(
                root_node, module_qn, language, queries
            )
            self._ingest_prototype_inheritance(root_node, module_qn, language, queries)

            return (root_node, language)

        except Exception as e:
            logger.error(f"Failed to parse or ingest {file_path}: {e}")
            return None

    def process_dependencies(self, filepath: Path) -> None:
        logger.info(f"  Parsing dependency file: {filepath}")

        dependencies = parse_dependencies(filepath)
        for dep in dependencies:
            self._add_dependency(dep.name, dep.spec, dep.properties)

    def _add_dependency(
        self, dep_name: str, dep_spec: str, properties: dict[str, str] | None = None
    ) -> None:
        if not dep_name or dep_name.lower() in {"python", "php"}:
            return

        logger.info(f"    Found dependency: {dep_name} (spec: {dep_spec})")
        self.ingestor.ensure_node_batch("ExternalPackage", {"name": dep_name})

        rel_properties = {"version_spec": dep_spec} if dep_spec else {}
        if properties:
            rel_properties |= properties

        self.ingestor.ensure_relationship_batch(
            ("Project", "name", self.project_name),
            "DEPENDS_ON_EXTERNAL",
            ("ExternalPackage", "name", dep_name),
            properties=rel_properties,
        )

    def _get_docstring(self, node: ASTNode) -> str | None:
        body_node = node.child_by_field_name("body")
        if not body_node or not body_node.children:
            return None
        first_statement = body_node.children[0]
        if (
            first_statement.type == "expression_statement"
            and first_statement.children[0].type == "string"
        ):
            text = first_statement.children[0].text
            if text is not None:
                result: str = safe_decode_with_fallback(
                    first_statement.children[0]
                ).strip("'\" \n")
                return result
        return None

    def _extract_decorators(self, node: ASTNode) -> list[str]:
        decorators: list[str] = []

        current = node.parent
        while current:
            if current.type == "decorated_definition":
                for child in current.children:
                    if child.type == "decorator":
                        if decorator_name := self._get_decorator_name(child):
                            decorators.append(decorator_name)
                break
            current = current.parent

        return decorators

    def _get_decorator_name(self, decorator_node: ASTNode) -> str | None:
        from .utils import safe_decode_text

        for child in decorator_node.children:
            if child.type == "identifier":
                return safe_decode_text(child)
            if child.type == "attribute":
                return safe_decode_text(child)
            if child.type == "call":
                if func_node := child.child_by_field_name("function"):
                    if func_node.type in ["identifier", "attribute"]:
                        return safe_decode_text(func_node)
        return None
