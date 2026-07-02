//! Single-server endpoint checking: classify a live HTTP response against
//! the openapi-declared expectations for one (path, method).

use std::collections::BTreeSet;

use serde_json::Value;

use crate::openapi::OperationInfo;

/// Outcome of probing one endpoint against a single running server.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Outcome {
    /// 2xx and (if a schema was declared) all expected fields present.
    Ok,
    /// Server returned 404 where the contract expects a route to exist.
    Missing,
    /// Server responded but is missing declared top-level fields, or the
    /// declared shape (object vs array) doesn't match what came back.
    ShapeMismatch { missing_fields: Vec<String> },
    /// Non-404 non-2xx status, or a request/transport failure.
    Error { detail: String },
    /// Path has `{param}` segments and no fixture was supplied.
    SkippedNeedsParam,
}

impl Outcome {
    pub fn label(&self) -> &'static str {
        match self {
            Outcome::Ok => "OK",
            Outcome::Missing => "MISSING",
            Outcome::ShapeMismatch { .. } => "SHAPE-MISMATCH",
            Outcome::Error { .. } => "ERROR",
            Outcome::SkippedNeedsParam => "SKIPPED",
        }
    }
}

/// Given an HTTP status + parsed JSON body (if any) and the operation's
/// declared expectations, classify the result. `body` is `None` when the
/// response wasn't valid JSON (or was empty) — status alone still lets us
/// distinguish MISSING/ERROR/OK.
pub fn classify_response(status: u16, body: Option<&Value>, info: &OperationInfo) -> Outcome {
    if status == 404 {
        return Outcome::Missing;
    }
    if !(200..300).contains(&status) {
        return Outcome::Error {
            detail: format!("HTTP {status}"),
        };
    }
    if info.expected_fields.is_empty() {
        return Outcome::Ok;
    }

    let Some(body) = body else {
        return Outcome::ShapeMismatch {
            missing_fields: info.expected_fields.iter().cloned().collect(),
        };
    };

    let target = if info.is_array {
        // Check shape against the first element, if any; an empty array is
        // vacuously fine (nothing to check).
        match body.as_array().and_then(|a| a.first()) {
            Some(first) => first,
            None => return Outcome::Ok,
        }
    } else {
        body
    };

    let Some(obj) = target.as_object() else {
        return Outcome::ShapeMismatch {
            missing_fields: info.expected_fields.iter().cloned().collect(),
        };
    };

    let present: BTreeSet<&str> = obj.keys().map(|k| k.as_str()).collect();
    let missing: Vec<String> = info
        .expected_fields
        .iter()
        .filter(|f| !present.contains(f.as_str()))
        .cloned()
        .collect();

    if missing.is_empty() {
        Outcome::Ok
    } else {
        Outcome::ShapeMismatch {
            missing_fields: missing,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn info_with_fields(fields: &[&str]) -> OperationInfo {
        OperationInfo {
            expected_fields: fields.iter().map(|s| s.to_string()).collect(),
            is_array: false,
        }
    }

    #[test]
    fn status_404_is_missing_regardless_of_body() {
        let info = info_with_fields(&["alias"]);
        assert_eq!(classify_response(404, None, &info), Outcome::Missing);
    }

    #[test]
    fn non_2xx_non_404_is_error() {
        let info = OperationInfo::default();
        let outcome = classify_response(500, None, &info);
        assert!(matches!(outcome, Outcome::Error { .. }));
    }

    #[test]
    fn no_declared_schema_is_ok_on_2xx() {
        let info = OperationInfo::default();
        let body = json!({"anything": "goes"});
        assert_eq!(classify_response(200, Some(&body), &info), Outcome::Ok);
    }

    #[test]
    fn all_expected_fields_present_is_ok() {
        let info = info_with_fields(&["alias", "destination"]);
        let body = json!({"alias": "a", "destination": "b", "extra": 1});
        assert_eq!(classify_response(200, Some(&body), &info), Outcome::Ok);
    }

    #[test]
    fn missing_expected_field_is_shape_mismatch() {
        let info = info_with_fields(&["alias", "destination"]);
        let body = json!({"alias": "a"});
        match classify_response(200, Some(&body), &info) {
            Outcome::ShapeMismatch { missing_fields } => {
                assert_eq!(missing_fields, vec!["destination".to_string()]);
            }
            other => panic!("expected ShapeMismatch, got {other:?}"),
        }
    }

    #[test]
    fn array_schema_checks_first_element() {
        let info = OperationInfo {
            expected_fields: ["integration_id", "project"]
                .into_iter()
                .map(String::from)
                .collect(),
            is_array: true,
        };
        let body = json!([{"integration_id": "x"}]);
        match classify_response(200, Some(&body), &info) {
            Outcome::ShapeMismatch { missing_fields } => {
                assert_eq!(missing_fields, vec!["project".to_string()]);
            }
            other => panic!("expected ShapeMismatch, got {other:?}"),
        }
    }

    #[test]
    fn empty_array_with_declared_schema_is_ok() {
        let info = OperationInfo {
            expected_fields: ["integration_id"].into_iter().map(String::from).collect(),
            is_array: true,
        };
        let body = json!([]);
        assert_eq!(classify_response(200, Some(&body), &info), Outcome::Ok);
    }

    #[test]
    fn missing_body_with_declared_schema_is_shape_mismatch() {
        let info = info_with_fields(&["alias"]);
        assert!(matches!(
            classify_response(200, None, &info),
            Outcome::ShapeMismatch { .. }
        ));
    }

    #[test]
    fn non_object_body_with_declared_schema_is_shape_mismatch() {
        let info = info_with_fields(&["alias"]);
        let body = json!("just a string");
        assert!(matches!(
            classify_response(200, Some(&body), &info),
            Outcome::ShapeMismatch { .. }
        ));
    }
}
