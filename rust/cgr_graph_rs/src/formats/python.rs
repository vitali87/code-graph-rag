//! The Python face of `formats`: `xml_find_all`, `toml_dumps`, and the
//! exceptions they raise.

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyMapping, PyString, PyTuple};

use super::toml::{self, Value};
use super::xml::{self, Error};

pyo3::create_exception!(
    cgr_graph_rs,
    XmlSyntaxError,
    PyValueError,
    "An XML document that is not well-formed, or outside what the reader reads."
);
pyo3::create_exception!(
    cgr_graph_rs,
    XmlEntityError,
    PyValueError,
    "An XML document whose DTD declares an entity; it is never expanded."
);

/// Deeper than tomli-w could nest before Python's recursion limit.
const MAX_TOML_DEPTH: usize = 256;

/// `[dict(e.attrib) for e in root.iter(name)]` for the XML document in
/// `data`. Raises `XmlSyntaxError` or `XmlEntityError`.
#[pyfunction]
pub fn xml_find_all<'py>(py: Python<'py>, data: &[u8], name: &str) -> PyResult<Bound<'py, PyList>> {
    let found = xml::find_all(data, name).map_err(|error| match error {
        Error::Syntax { .. } => XmlSyntaxError::new_err(error.to_string()),
        Error::EntityDeclared { .. } => XmlEntityError::new_err(error.to_string()),
    })?;
    let out = PyList::empty(py);
    for attributes in found {
        let dict = PyDict::new(py);
        for (key, value) in attributes {
            dict.set_item(key, value)?;
        }
        out.append(dict)?;
    }
    Ok(out)
}

/// `tomli_w.dumps(table)` for tables of strings, booleans, ints, floats,
/// lists, tuples and mappings; anything else raises `TypeError`, as
/// tomli-w does.
#[pyfunction]
pub fn toml_dumps(table: &Bound<'_, PyAny>) -> PyResult<String> {
    Ok(toml::dumps(&entries(table, 0)?))
}

fn entries(table: &Bound<'_, PyAny>, depth: usize) -> PyResult<Vec<(String, Value)>> {
    if depth > MAX_TOML_DEPTH {
        return Err(PyValueError::new_err("TOML table nested too deeply"));
    }
    let items = match table.cast::<PyDict>() {
        Ok(dict) => dict.items(),
        Err(_) => table.cast::<PyMapping>()?.items()?,
    };
    let mut out = Vec::with_capacity(items.len());
    for item in items.iter() {
        let (key, value): (Bound<'_, PyAny>, Bound<'_, PyAny>) = item.extract()?;
        let Ok(key) = key.cast::<PyString>() else {
            return Err(PyTypeError::new_err(format!(
                "Invalid mapping key '{}' of type '{}'. A string is required.",
                key.str()?,
                key.get_type().qualname()?,
            )));
        };
        out.push((key.to_str()?.to_string(), convert(&value, depth + 1)?));
    }
    Ok(out)
}

/// The order of tomli-w's `format_literal`, isinstance checks included.
fn convert(value: &Bound<'_, PyAny>, depth: usize) -> PyResult<Value> {
    if value.is_instance_of::<PyBool>() {
        return Ok(Value::Bool(value.is_truthy()?));
    }
    if value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>() {
        return Ok(Value::Number(value.str()?.to_str()?.to_string()));
    }
    if let Ok(text) = value.cast::<PyString>() {
        return Ok(Value::Str(text.to_str()?.to_string()));
    }
    if value.is_instance_of::<PyList>() || value.is_instance_of::<PyTuple>() {
        if depth > MAX_TOML_DEPTH {
            return Err(PyValueError::new_err("TOML array nested too deeply"));
        }
        let items = value
            .try_iter()?
            .map(|item| convert(&item?, depth + 1))
            .collect::<PyResult<Vec<_>>>()?;
        return Ok(Value::Array(items));
    }
    if value.is_instance_of::<PyDict>() || value.cast::<PyMapping>().is_ok() {
        return Ok(Value::Table(entries(value, depth)?));
    }
    Err(PyTypeError::new_err(format!(
        "Object of type '{}' is not TOML serializable",
        value.get_type().qualname()?,
    )))
}

/// Adds the functions and exceptions to the extension module.
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(xml_find_all, m)?)?;
    m.add_function(wrap_pyfunction!(toml_dumps, m)?)?;
    m.add("XmlSyntaxError", m.py().get_type::<XmlSyntaxError>())?;
    m.add("XmlEntityError", m.py().get_type::<XmlEntityError>())
}
