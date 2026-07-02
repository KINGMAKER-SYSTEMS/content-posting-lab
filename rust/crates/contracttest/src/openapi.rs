//! Parser for `rust/contract/openapi.json`.
//!
//! Extracts, for each (path, method), the set of expected top-level response
//! fields from `responses.200.content.application/json.schema` — resolving a
//! single level of `$ref` against `components.schemas`. Most routes in this
//! codebase's FastAPI app don't declare a `response_model`, so their schema is
//! `{}` (empty) — in that case we only assert on HTTP status, never on shape.
//!
//! Also exposes path-template parsing (`{name}` segments) so callers can
//! decide whether a path needs fixture substitution.

use std::collections::{BTreeMap, BTreeSet};

use serde_json::Value;

/// Everything the harness needs to know about one (method, path) operation.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct OperationInfo {
    /// Top-level field names expected in the 200 JSON response body.
    /// Empty means "no declared schema — status-only check."
    pub expected_fields: BTreeSet<String>,
    /// True if the schema resolved to a JSON array (`type: array`) rather
    /// than an object — expected_fields then describes the *item* schema.
    pub is_array: bool,
}

/// Parsed openapi.json, keyed by (path, UPPERCASE method).
#[derive(Debug, Default)]
pub struct OpenApiSpec {
    pub operations: BTreeMap<(String, String), OperationInfo>,
}

impl OpenApiSpec {
    pub fn parse(root: &Value) -> Self {
        let mut operations = BTreeMap::new();
        let schemas = root
            .get("components")
            .and_then(|c| c.get("schemas"))
            .cloned()
            .unwrap_or(Value::Null);

        let Some(paths) = root.get("paths").and_then(|p| p.as_object()) else {
            return OpenApiSpec { operations };
        };

        for (path, methods) in paths {
            let Some(methods) = methods.as_object() else {
                continue;
            };
            for (method, op) in methods {
                // openapi.json path items can carry non-method keys
                // (parameters, summary, etc) — skip anything not a verb.
                if !is_http_method(method) {
                    continue;
                }
                let schema = op
                    .get("responses")
                    .and_then(|r| r.get("200"))
                    .and_then(|r| r.get("content"))
                    .and_then(|c| c.get("application/json"))
                    .and_then(|c| c.get("schema"));

                let info = schema
                    .map(|s| resolve_schema_fields(s, &schemas))
                    .unwrap_or_default();

                operations.insert((path.clone(), method.to_ascii_uppercase()), info);
            }
        }

        OpenApiSpec { operations }
    }

    pub fn get(&self, path: &str, method: &str) -> Option<&OperationInfo> {
        self.operations
            .get(&(path.to_string(), method.to_ascii_uppercase()))
    }
}

fn is_http_method(s: &str) -> bool {
    matches!(
        s.to_ascii_lowercase().as_str(),
        "get" | "post" | "put" | "patch" | "delete" | "options" | "head" | "trace"
    )
}

/// Resolve a schema value (possibly a `$ref`) into a set of expected
/// top-level field names, following exactly one level of `$ref` indirection
/// (sufficient for this spec — components.schemas entries here don't nest
/// further refs at the top level) plus one level of `items.$ref` for arrays.
fn resolve_schema_fields(schema: &Value, components: &Value) -> OperationInfo {
    let resolved = deref(schema, components);

    if let Some(items) = resolved.get("items") {
        let item_resolved = deref(items, components);
        return OperationInfo {
            expected_fields: field_names(&item_resolved),
            is_array: true,
        };
    }

    OperationInfo {
        expected_fields: field_names(&resolved),
        is_array: false,
    }
}

fn deref<'a>(schema: &'a Value, components: &'a Value) -> Value {
    if let Some(ref_path) = schema.get("$ref").and_then(|r| r.as_str()) {
        if let Some(name) = ref_path.strip_prefix("#/components/schemas/") {
            if let Some(target) = components.get(name) {
                return target.clone();
            }
        }
    }
    schema.clone()
}

fn field_names(schema: &Value) -> BTreeSet<String> {
    schema
        .get("properties")
        .and_then(|p| p.as_object())
        .map(|props| props.keys().cloned().collect())
        .unwrap_or_default()
}

/// Split a path template like `/api/burn/batch-status/{batch_id}` into
/// literal segments and `{param}` placeholders.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PathSegment {
    Literal(String),
    Param(String),
}

pub fn parse_path_template(path: &str) -> Vec<PathSegment> {
    path.split('/')
        .filter(|s| !s.is_empty())
        .map(|seg| {
            if seg.starts_with('{') && seg.ends_with('}') {
                PathSegment::Param(seg[1..seg.len() - 1].to_string())
            } else {
                PathSegment::Literal(seg.to_string())
            }
        })
        .collect()
}

/// True if the path template has at least one `{param}` segment.
pub fn has_path_params(path: &str) -> bool {
    parse_path_template(path)
        .iter()
        .any(|s| matches!(s, PathSegment::Param(_)))
}

/// Substitute `{param}` segments using a fixtures map (param name -> value).
/// Returns None if any param segment has no fixture entry.
pub fn substitute_path_params(
    path: &str,
    fixtures: &BTreeMap<String, String>,
) -> Option<String> {
    let mut out = String::new();
    for seg in parse_path_template(path) {
        out.push('/');
        match seg {
            PathSegment::Literal(l) => out.push_str(&l),
            PathSegment::Param(p) => {
                out.push_str(fixtures.get(&p)?);
            }
        }
    }
    Some(out)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn sample_spec() -> Value {
        json!({
            "paths": {
                "/api/health": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": { "schema": {} }
                                }
                            }
                        }
                    }
                },
                "/api/pipeline/mint-alias": {
                    "post": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": { "$ref": "#/components/schemas/MintAliasResponse" }
                                    }
                                }
                            }
                        }
                    }
                },
                "/api/roster/": {
                    "get": {
                        "responses": {
                            "200": {
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "array",
                                            "items": { "$ref": "#/components/schemas/RosterEntry" }
                                        }
                                    }
                                }
                            }
                        }
                    },
                    "parameters": [], // non-method key alongside "get"
                }
            },
            "components": {
                "schemas": {
                    "MintAliasResponse": {
                        "type": "object",
                        "properties": {
                            "alias": {"type": "string"},
                            "destination": {"type": "string"},
                            "notion_page_id": {"type": "string"}
                        },
                        "required": ["alias", "destination"]
                    },
                    "RosterEntry": {
                        "type": "object",
                        "properties": {
                            "integration_id": {"type": "string"},
                            "project": {"type": "string"}
                        }
                    }
                }
            }
        })
    }

    #[test]
    fn empty_schema_yields_no_expected_fields() {
        let spec = OpenApiSpec::parse(&sample_spec());
        let info = spec.get("/api/health", "GET").unwrap();
        assert!(info.expected_fields.is_empty());
        assert!(!info.is_array);
    }

    #[test]
    fn resolves_ref_to_field_names() {
        let spec = OpenApiSpec::parse(&sample_spec());
        let info = spec.get("/api/pipeline/mint-alias", "POST").unwrap();
        let expected: BTreeSet<String> = ["alias", "destination", "notion_page_id"]
            .into_iter()
            .map(String::from)
            .collect();
        assert_eq!(info.expected_fields, expected);
        assert!(!info.is_array);
    }

    #[test]
    fn resolves_array_of_ref_items() {
        let spec = OpenApiSpec::parse(&sample_spec());
        let info = spec.get("/api/roster/", "GET").unwrap();
        assert!(info.is_array);
        let expected: BTreeSet<String> = ["integration_id", "project"]
            .into_iter()
            .map(String::from)
            .collect();
        assert_eq!(info.expected_fields, expected);
    }

    #[test]
    fn ignores_non_method_keys_in_path_item() {
        let spec = OpenApiSpec::parse(&sample_spec());
        // "parameters" key on /api/roster/ path item must not become a fake method
        assert!(spec.get("/api/roster/", "PARAMETERS").is_none());
    }

    #[test]
    fn path_template_parsing() {
        let segs = parse_path_template("/api/burn/batch-status/{batch_id}");
        assert_eq!(
            segs,
            vec![
                PathSegment::Literal("api".into()),
                PathSegment::Literal("burn".into()),
                PathSegment::Literal("batch-status".into()),
                PathSegment::Param("batch_id".into()),
            ]
        );
        assert!(has_path_params("/api/burn/batch-status/{batch_id}"));
        assert!(!has_path_params("/api/health"));
    }

    #[test]
    fn substitute_params_from_fixtures() {
        let mut fixtures = BTreeMap::new();
        fixtures.insert("batch_id".to_string(), "abc123".to_string());
        let out = substitute_path_params("/api/burn/batch-status/{batch_id}", &fixtures);
        assert_eq!(out, Some("/api/burn/batch-status/abc123".to_string()));

        let empty = BTreeMap::new();
        assert_eq!(
            substitute_path_params("/api/burn/batch-status/{batch_id}", &empty),
            None
        );
    }
}
