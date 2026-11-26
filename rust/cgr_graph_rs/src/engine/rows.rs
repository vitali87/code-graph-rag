//! Rows handed to the ingestor (docs/reports/PHASE2_FRAME.md, section 9).
//!
//! The frame builds node and relationship rows; Python's ingestor writes
//! them, through `ensure_node_batch(label, properties)` and
//! `ensure_relationship_batch(start, rel_type, end)`, so the writer's rules
//! stay in one place. The names below are the graph schema's
//! (`codebase_rag/constants/graph.py`), as `Engine` exposes them for
//! `codebase_rag/tests/test_engine_structure.py` to hold to Python's.

use std::ffi::OsString;

/// `cs.NodeLabel.PROJECT`, `PACKAGE`, `FOLDER` and `FILE`.
pub const PROJECT: &str = "Project";
pub const PACKAGE: &str = "Package";
pub const FOLDER: &str = "Folder";
pub const FILE: &str = "File";
/// The labels the frame's rows carry, in that order.
pub const NODE_LABELS: &[&str] = &[PROJECT, PACKAGE, FOLDER, FILE];

/// `cs.KEY_NAME`, `KEY_QUALIFIED_NAME`, `KEY_PATH`, `KEY_ABSOLUTE_PATH`
/// and `KEY_EXTENSION`.
pub const NAME: &str = "name";
pub const QUALIFIED_NAME: &str = "qualified_name";
pub const PATH: &str = "path";
pub const ABSOLUTE_PATH: &str = "absolute_path";
pub const EXTENSION: &str = "extension";
/// The property keys the frame's rows carry, in that order.
pub const PROPERTY_KEYS: &[&str] = &[NAME, QUALIFIED_NAME, PATH, ABSOLUTE_PATH, EXTENSION];

/// `cs.RelationshipType.CONTAINS_PACKAGE`, `CONTAINS_FOLDER` and
/// `CONTAINS_FILE`.
pub const CONTAINS_PACKAGE: &str = "CONTAINS_PACKAGE";
pub const CONTAINS_FOLDER: &str = "CONTAINS_FOLDER";
pub const CONTAINS_FILE: &str = "CONTAINS_FILE";
/// The relationship types the frame's rows carry, in that order.
pub const RELATIONSHIP_TYPES: &[&str] = &[CONTAINS_PACKAGE, CONTAINS_FOLDER, CONTAINS_FILE];

/// A node as an edge names it: `(label, key, value)`, a `NodeIdentifier`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NodeId {
    pub label: &'static str,
    pub key: &'static str,
    pub value: OsString,
}

/// A node row: its label and its properties, in the order Python's dict
/// holds them. A value is a path or a name, so it keeps a path's own bytes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NodeRow {
    pub label: &'static str,
    pub properties: Vec<(&'static str, OsString)>,
}

/// A relationship row: `start -[rel_type]-> end`.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RelationshipRow {
    pub start: NodeId,
    pub rel_type: &'static str,
    pub end: NodeId,
}

/// A node and the edge from what contains it, written in that order: what
/// the structure pass writes for each package, folder and file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContainedRow {
    pub node: NodeRow,
    pub relationship: RelationshipRow,
}

impl ContainedRow {
    /// `node`, and the edge `parent -[rel_type]-> node` naming the node by
    /// its property `key`, which `node` must hold.
    pub fn new(node: NodeRow, parent: NodeId, rel_type: &'static str, key: &'static str) -> Self {
        let value = node
            .properties
            .iter()
            .find(|(name, _)| *name == key)
            .map(|(_, value)| value.clone())
            .unwrap_or_default();
        let end = NodeId {
            label: node.label,
            key,
            value,
        };
        ContainedRow {
            node,
            relationship: RelationshipRow {
                start: parent,
                rel_type,
                end,
            },
        }
    }
}
