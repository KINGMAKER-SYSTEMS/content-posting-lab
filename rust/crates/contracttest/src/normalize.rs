//! Response normalization + structural diff for the `diff` subcommand.
//!
//! Two servers (Rust vs Python) implementing the "same" endpoint will never
//! byte-for-byte match: timestamps differ, UUIDs/ids differ, array ordering
//! may differ. What we actually care about for contract parity is: same
//! *shape* — same key sets at every level, same JSON *type* per key. We
//! normalize away volatile leaf values, then compare the resulting shape
//! trees for structural equality.

use std::collections::BTreeSet;

use serde_json::Value;

/// Field names considered volatile at any depth — their VALUE is ignored,
/// but their presence/absence still matters (that's still shape).
const VOLATILE_KEYS: &[&str] = &[
    "id",
    "ids",
    "job_id",
    "batch_id",
    "request_id",
    "sound_id",
    "poster_id",
    "integration_id",
    "rule_id",
    "note_id",
    "user_id",
    "uuid",
    "created_at",
    "updated_at",
    "timestamp",
    "time",
    "date",
    "started_at",
    "finished_at",
    "completed_at",
];

fn is_volatile_key(key: &str) -> bool {
    let lower = key.to_ascii_lowercase();
    VOLATILE_KEYS.iter().any(|v| *v == lower)
        || lower.ends_with("_id")
        || lower.ends_with("_at")
}

/// A shape descriptor: structurally comparable, values stripped.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Shape {
    Null,
    Bool,
    Number,
    String,
    /// Object: sorted (key, shape) pairs. Volatile keys keep their key but
    /// their shape is collapsed to `Volatile` so type drift on a volatile
    /// field (e.g. id as int vs string) doesn't fail the diff.
    Object(Vec<(String, Shape)>),
    /// Array: for parity purposes we only compare the shape of elements,
    /// normalized to a single merged shape (order-independent) — an empty
    /// array has no element shape.
    Array(Option<Box<Shape>>),
    /// A value under a volatile key — presence checked, content ignored.
    Volatile,
}

/// Normalize a JSON value into a comparable `Shape` tree.
pub fn normalize(value: &Value) -> Shape {
    normalize_inner(value, false)
}

fn normalize_inner(value: &Value, parent_volatile: bool) -> Shape {
    if parent_volatile {
        return Shape::Volatile;
    }
    match value {
        Value::Null => Shape::Null,
        Value::Bool(_) => Shape::Bool,
        Value::Number(_) => Shape::Number,
        Value::String(_) => Shape::String,
        Value::Array(items) => {
            if items.is_empty() {
                Shape::Array(None)
            } else {
                // Merge all element shapes; if they disagree, that's still
                // useful info, but for v1 we just take the first — arrays in
                // this API are homogeneous lists of one record type.
                let first = normalize_inner(&items[0], false);
                Shape::Array(Some(Box::new(first)))
            }
        }
        Value::Object(map) => {
            let mut fields: Vec<(String, Shape)> = map
                .iter()
                .map(|(k, v)| {
                    let shape = if is_volatile_key(k) {
                        Shape::Volatile
                    } else {
                        normalize_inner(v, false)
                    };
                    (k.clone(), shape)
                })
                .collect();
            fields.sort_by(|a, b| a.0.cmp(&b.0));
            Shape::Object(fields)
        }
    }
}

/// A single structural discrepancy found between two shapes.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShapeDiff {
    pub path: String,
    pub kind: DiffKind,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DiffKind {
    /// Key present on one side only.
    MissingKey { side: Side },
    /// Same key, different JSON type / shape.
    TypeMismatch { rust: String, python: String },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Side {
    Rust,
    Python,
}

/// Compare two normalized shapes, collecting all discrepancies with
/// dotted-path breadcrumbs (e.g. `data.items[].name`).
pub fn diff_shapes(rust: &Shape, python: &Shape) -> Vec<ShapeDiff> {
    let mut out = Vec::new();
    diff_at("$", rust, python, &mut out);
    out
}

fn diff_at(path: &str, rust: &Shape, python: &Shape, out: &mut Vec<ShapeDiff>) {
    match (rust, python) {
        (Shape::Volatile, _) | (_, Shape::Volatile) => {}
        (Shape::Object(r_fields), Shape::Object(p_fields)) => {
            let r_keys: BTreeSet<&str> = r_fields.iter().map(|(k, _)| k.as_str()).collect();
            let p_keys: BTreeSet<&str> = p_fields.iter().map(|(k, _)| k.as_str()).collect();

            for key in r_keys.difference(&p_keys) {
                out.push(ShapeDiff {
                    path: format!("{path}.{key}"),
                    kind: DiffKind::MissingKey { side: Side::Python },
                });
            }
            for key in p_keys.difference(&r_keys) {
                out.push(ShapeDiff {
                    path: format!("{path}.{key}"),
                    kind: DiffKind::MissingKey { side: Side::Rust },
                });
            }
            for key in r_keys.intersection(&p_keys) {
                let r_shape = &r_fields.iter().find(|(k, _)| k == key).unwrap().1;
                let p_shape = &p_fields.iter().find(|(k, _)| k == key).unwrap().1;
                diff_at(&format!("{path}.{key}"), r_shape, p_shape, out);
            }
        }
        (Shape::Array(r_elem), Shape::Array(p_elem)) => {
            // If either side's array is empty there's no element shape to
            // compare structurally — only compare when both are non-empty.
            if let (Some(r), Some(p)) = (r_elem, p_elem) {
                diff_at(&format!("{path}[]"), r, p, out)
            }
        }
        (r, p) if r == p => {}
        (r, p) => out.push(ShapeDiff {
            path: path.to_string(),
            kind: DiffKind::TypeMismatch {
                rust: shape_kind_name(r),
                python: shape_kind_name(p),
            },
        }),
    }
}

fn shape_kind_name(s: &Shape) -> String {
    match s {
        Shape::Null => "null",
        Shape::Bool => "bool",
        Shape::Number => "number",
        Shape::String => "string",
        Shape::Object(_) => "object",
        Shape::Array(_) => "array",
        Shape::Volatile => "volatile",
    }
    .to_string()
}

/// Convenience: normalize + diff two raw JSON response bodies directly.
pub fn diff_responses(rust: &Value, python: &Value) -> Vec<ShapeDiff> {
    diff_shapes(&normalize(rust), &normalize(python))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn identical_bodies_have_no_diff() {
        let a = json!({"name": "x", "count": 1, "items": [{"a": 1}]});
        let b = json!({"name": "y", "count": 2, "items": [{"a": 9}]});
        assert!(diff_responses(&a, &b).is_empty());
    }

    #[test]
    fn volatile_keys_ignore_type_drift() {
        // "created_at" as string on one side, number (epoch) on the other —
        // still fine, it's volatile.
        let a = json!({"created_at": "2026-01-01T00:00:00Z", "name": "x"});
        let b = json!({"created_at": 1735689600, "name": "y"});
        assert!(diff_responses(&a, &b).is_empty());
    }

    #[test]
    fn missing_key_on_python_side_detected() {
        let a = json!({"name": "x", "extra_field": true});
        let b = json!({"name": "y"});
        let diffs = diff_responses(&a, &b);
        assert_eq!(diffs.len(), 1);
        assert_eq!(diffs[0].path, "$.extra_field");
        assert!(matches!(
            diffs[0].kind,
            DiffKind::MissingKey { side: Side::Python }
        ));
    }

    #[test]
    fn missing_key_on_rust_side_detected() {
        let a = json!({"name": "x"});
        let b = json!({"name": "y", "extra_field": true});
        let diffs = diff_responses(&a, &b);
        assert_eq!(diffs.len(), 1);
        assert!(matches!(
            diffs[0].kind,
            DiffKind::MissingKey { side: Side::Rust }
        ));
    }

    #[test]
    fn type_mismatch_detected() {
        let a = json!({"count": 5});
        let b = json!({"count": "five"});
        let diffs = diff_responses(&a, &b);
        assert_eq!(diffs.len(), 1);
        assert_eq!(diffs[0].path, "$.count");
        match &diffs[0].kind {
            DiffKind::TypeMismatch { rust, python } => {
                assert_eq!(rust, "number");
                assert_eq!(python, "string");
            }
            _ => panic!("expected TypeMismatch"),
        }
    }

    #[test]
    fn nested_object_diff_has_dotted_path() {
        let a = json!({"data": {"page": {"title": "x", "slug": "y"}}});
        let b = json!({"data": {"page": {"title": "x"}}});
        let diffs = diff_responses(&a, &b);
        assert_eq!(diffs.len(), 1);
        assert_eq!(diffs[0].path, "$.data.page.slug");
    }

    #[test]
    fn array_element_shape_compared() {
        let a = json!({"items": [{"name": "x", "count": 1}]});
        let b = json!({"items": [{"name": "y"}]});
        let diffs = diff_responses(&a, &b);
        assert_eq!(diffs.len(), 1);
        assert_eq!(diffs[0].path, "$.items[].count");
    }

    #[test]
    fn empty_arrays_on_either_side_are_not_compared() {
        let a = json!({"items": []});
        let b = json!({"items": [{"name": "y"}]});
        assert!(diff_responses(&a, &b).is_empty());
    }

    #[test]
    fn ordering_of_object_keys_does_not_matter() {
        let a = json!({"b": 1, "a": 2});
        let b = json!({"a": 9, "b": 8});
        assert!(diff_responses(&a, &b).is_empty());
    }
}
