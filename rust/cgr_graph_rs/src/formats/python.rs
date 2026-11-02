//! The Python face of `formats`: `xml_find_all` and `toml_dumps`.
//!
//! Each returns None where the Python reference must answer instead
//! (`codebase_rag/utils/safe_xml.py`, `codebase_rag/utils/toml_writer.py`).

use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyInt, PyList, PyString, PyTuple};

use super::toml::{self, Value};
use super::xml;

/// Deeper than any workspace file nests; past it Python answers, so the
/// conversion's recursion stays shallow.
const MAX_TOML_DEPTH: usize = 32;

/// `[dict(e.attrib) for e in root.iter(name)]` for the document in `data`,
/// or None when the safe parser in Python must read it.
#[pyfunction]
pub fn xml_find_all<'py>(
    py: Python<'py>,
    data: &[u8],
    name: &str,
) -> PyResult<Option<Bound<'py, PyList>>> {
    let Some(found) = xml::find_all(data, name) else {
        return Ok(None);
    };
    let out = PyList::empty(py);
    for attributes in found {
        let dict = PyDict::new(py);
        for (key, value) in attributes {
            dict.set_item(key, value)?;
        }
        out.append(dict)?;
    }
    Ok(Some(out))
}

/// `tomli_w.dumps(table)`, or None when `table` holds anything but exact
/// dicts, lists, tuples, strings, booleans and 64-bit integers.
#[pyfunction]
pub fn toml_dumps(table: &Bound<'_, PyAny>) -> PyResult<Option<String>> {
    Ok(entries(table, 0).map(|entries| toml::dumps(&entries)))
}

fn entries(table: &Bound<'_, PyAny>, depth: usize) -> Option<Vec<(String, Value)>> {
    if depth > MAX_TOML_DEPTH || !table.is_exact_instance_of::<PyDict>() {
        return None;
    }
    let table = table.cast::<PyDict>().ok()?;
    let mut out = Vec::with_capacity(table.len());
    for (key, value) in table.iter() {
        if !key.is_exact_instance_of::<PyString>() {
            return None;
        }
        let key = key.cast::<PyString>().ok()?.to_str().ok()?.to_string();
        out.push((key, convert(&value, depth + 1)?));
    }
    Some(out)
}

fn convert(value: &Bound<'_, PyAny>, depth: usize) -> Option<Value> {
    if value.is_exact_instance_of::<PyBool>() {
        return value.extract::<bool>().ok().map(Value::Bool);
    }
    if value.is_exact_instance_of::<PyInt>() {
        return value.extract::<i64>().ok().map(Value::Int);
    }
    if value.is_exact_instance_of::<PyString>() {
        let text = value.cast::<PyString>().ok()?.to_str().ok()?;
        return Some(Value::Str(text.to_string()));
    }
    if value.is_exact_instance_of::<PyDict>() {
        return entries(value, depth).map(Value::Table);
    }
    if value.is_exact_instance_of::<PyList>() || value.is_exact_instance_of::<PyTuple>() {
        if depth > MAX_TOML_DEPTH {
            return None;
        }
        let items = value
            .try_iter()
            .ok()?
            .map(|item| convert(&item.ok()?, depth + 1))
            .collect::<Option<Vec<_>>>()?;
        return Some(Value::Array(items));
    }
    None
}

/// Adds `xml_find_all` and `toml_dumps` to the extension module.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(xml_find_all, m)?)?;
    m.add_function(wrap_pyfunction!(toml_dumps, m)?)
}
