//! Yew/WASM Generate page — polished to production quality for the Yew-vs-React
//! evaluation. Imports the SAME `dto` structs the Axum server sends.
//!
//! Flow: load /api/providers → pick model + prompt + opts (+ optional image for
//! image-to-video) → POST /api/jobs/generate → poll /api/jobs/{id} to terminal
//! → show the generated asset's video. Prompt persists across reloads.

use dto::{
    Asset, AssetsResponse, GenerateRequest, GenerateResponse, JobResponse, JobStatus, ModelInfo,
    ProvidersResponse,
};
use gloo_net::http::Request;
use gloo_storage::{LocalStorage, Storage};
use gloo_timers::future::TimeoutFuture;
use wasm_bindgen_futures::spawn_local;
use web_sys::{HtmlInputElement, HtmlSelectElement, HtmlTextAreaElement};
use yew::prelude::*;

const PROJECT: &str = "yew-spike";
const PROMPT_KEY: &str = "generate.prompt";

#[derive(Clone, PartialEq)]
enum Phase {
    Idle,
    Submitting,
    Running { progress: f64, status: JobStatus },
    Done(Asset),
    Failed(String),
}

#[function_component(App)]
fn app() -> Html {
    let models = use_state(Vec::<ModelInfo>::new);
    let model = use_state(String::new);
    // Prompt persists across reloads — refreshing mid-thought shouldn't lose it.
    let prompt = use_state(|| LocalStorage::get::<String>(PROMPT_KEY).unwrap_or_default());
    let duration = use_state(|| 6u32);
    let aspect = use_state(|| "9:16".to_string());
    let resolution = use_state(|| "768p".to_string());
    let image = use_state(|| Option::<String>::None); // data URI
    let phase = use_state(|| Phase::Idle);
    let load_err = use_state(|| Option::<String>::None);

    {
        let (models, model, load_err) = (models.clone(), model.clone(), load_err.clone());
        use_effect_with((), move |_| {
            spawn_local(async move {
                match fetch_providers().await {
                    Ok(list) => {
                        if let Some(first) = list.first() {
                            model.set(first.id.clone());
                        }
                        models.set(list);
                    }
                    Err(e) => load_err.set(Some(e)),
                }
            });
            || ()
        });
    }

    let selected = models.iter().find(|m| m.id == *model).cloned();
    let needs_image = selected.as_ref().map(|m| m.needs_image).unwrap_or(false);

    let on_generate = {
        let (model, prompt, duration, aspect, resolution, image, phase) = (
            model.clone(), prompt.clone(), duration.clone(),
            aspect.clone(), resolution.clone(), image.clone(), phase.clone(),
        );
        Callback::from(move |_| {
            let req = GenerateRequest {
                project: PROJECT.to_string(),
                model: (*model).clone(),
                prompt: (*prompt).clone(),
                aspect_ratio: Some((*aspect).clone()),
                resolution: Some((*resolution).clone()),
                duration: Some(*duration),
                image_data_uri: (*image).clone(),
            };
            let phase = phase.clone();
            phase.set(Phase::Submitting);
            spawn_local(async move {
                match submit_generate(&req).await {
                    Ok(resp) => {
                        phase.set(Phase::Running { progress: 0.0, status: JobStatus::Queued });
                        poll_until_done(resp.job.id, phase).await;
                    }
                    Err(e) => phase.set(Phase::Failed(e)),
                }
            });
        })
    };

    let on_reset = {
        let (prompt, image, phase) = (prompt.clone(), image.clone(), phase.clone());
        Callback::from(move |_| {
            prompt.set(String::new());
            LocalStorage::delete(PROMPT_KEY);
            image.set(None);
            phase.set(Phase::Idle);
        })
    };

    let is_busy = matches!(*phase, Phase::Submitting | Phase::Running { .. });
    let image_ok = !needs_image || image.is_some();
    let can_submit = !prompt.trim().is_empty() && !model.is_empty() && image_ok && !is_busy;

    // ── input handlers ──
    let on_prompt = {
        let prompt = prompt.clone();
        Callback::from(move |e: InputEvent| {
            let v = e.target_unchecked_into::<HtmlTextAreaElement>().value();
            LocalStorage::set(PROMPT_KEY, &v).ok();
            prompt.set(v);
        })
    };
    let on_model = {
        let model = model.clone();
        Callback::from(move |e: Event| model.set(e.target_unchecked_into::<HtmlSelectElement>().value()))
    };
    let on_duration = {
        let duration = duration.clone();
        Callback::from(move |e: InputEvent| {
            if let Ok(v) = e.target_unchecked_into::<HtmlInputElement>().value().parse() { duration.set(v); }
        })
    };
    let on_aspect = {
        let aspect = aspect.clone();
        Callback::from(move |e: Event| aspect.set(e.target_unchecked_into::<HtmlSelectElement>().value()))
    };
    let on_resolution = {
        let resolution = resolution.clone();
        Callback::from(move |e: Event| resolution.set(e.target_unchecked_into::<HtmlSelectElement>().value()))
    };
    let on_file = {
        let image = image.clone();
        Callback::from(move |e: Event| {
            let input = e.target_unchecked_into::<HtmlInputElement>();
            if let Some(files) = input.files() {
                if let Some(file) = files.get(0) {
                    let image = image.clone();
                    let gf = gloo_file::File::from(file);
                    spawn_local(async move {
                        if let Ok(bytes) = gloo_file::futures::read_as_bytes(&gf).await {
                            let mime = gf.raw_mime_type();
                            let mime = if mime.is_empty() { "image/png".to_string() } else { mime };
                            let b64 = base64_encode(&bytes);
                            image.set(Some(format!("data:{mime};base64,{b64}")));
                        }
                    });
                }
            }
        })
    };

    html! {
        <div class="wrap">
            <div class="head">
                <h1><span class="gradient-text">{ "Generate" }</span></h1>
                <span class="step">{ "step 1 · create" }</span>
            </div>
            <p class="sub">{ "Describe a video; an AI model makes it. It becomes a source you can clip and burn." }</p>

            if let Some(e) = &*load_err {
                <div class="card"><p class="err">{ format!("Couldn't load models: {e}") }</p>
                    <p class="hint">{ "Is the API running with XAI_API_KEY or REPLICATE_API_TOKEN set?" }</p></div>
            }

            <div class="card">
                <div class="field">
                    <label class="label">{ "Model" }</label>
                    if models.is_empty() && load_err.is_none() {
                        <p class="muted">{ "Loading models…" }</p>
                    } else if models.is_empty() {
                        <p class="muted">{ "No models configured." }</p>
                    } else {
                        <select onchange={on_model}>
                            { for models.iter().map(|m| html!{
                                <option value={m.id.clone()} selected={m.id == *model}>
                                    { format!("{} · {}", m.name, m.group) }
                                </option>
                            }) }
                        </select>
                    }
                </div>

                <div class="field">
                    <label class="label">{ "Prompt" }</label>
                    <textarea oninput={on_prompt} value={(*prompt).clone()}
                        placeholder="a neon city skyline at night, slow cinematic drone shot" />
                </div>

                if needs_image {
                    <div class="field">
                        <label class="label">{ "First frame (image-to-video)" }</label>
                        <label class="drop">
                            if let Some(uri) = &*image {
                                <span>{ "Image attached · click to replace" }</span>
                                <img src={uri.clone()} alt="first frame" />
                            } else {
                                <span>{ "Click to upload a first-frame image" }</span>
                            }
                            <input type="file" accept="image/*" onchange={on_file} />
                        </label>
                        if image.is_none() {
                            <p class="hint warn">{ "This model needs a first-frame image to generate." }</p>
                        }
                    </div>
                }

                <div class="row">
                    <div class="field">
                        <label class="label">{ "Duration (s)" }</label>
                        <input type="number" min="1" max="15" value={duration.to_string()} oninput={on_duration} />
                    </div>
                    <div class="field">
                        <label class="label">{ "Aspect" }</label>
                        <select onchange={on_aspect}>
                            { for ["9:16","16:9","1:1"].iter().map(|a| html!{
                                <option value={*a} selected={*a == *aspect}>{ *a }</option>
                            }) }
                        </select>
                    </div>
                    <div class="field">
                        <label class="label">{ "Resolution" }</label>
                        <select onchange={on_resolution}>
                            { for ["768p","1080p","480p"].iter().map(|r| html!{
                                <option value={*r} selected={*r == *resolution}>{ *r }</option>
                            }) }
                        </select>
                    </div>
                </div>

                <div class="btn-row">
                    <button class="primary" onclick={on_generate} disabled={!can_submit}>
                        { if is_busy { "Generating…" } else { "Generate" } }
                    </button>
                    <button class="ghost" onclick={on_reset}>{ "Reset" }</button>
                </div>
            </div>

            { render_phase(&phase) }
        </div>
    }
}

fn render_phase(phase: &Phase) -> Html {
    match phase {
        Phase::Idle => html! {
            <div class="card empty"><span class="ico">{ "🎬" }</span>{ "Your generated video will appear here." }</div>
        },
        Phase::Submitting => html! {
            <div class="card"><span class="spin"></span>{ "Submitting to provider…" }</div>
        },
        Phase::Running { progress, status } => {
            let pct = (progress * 100.0).round() as i32;
            let (cls, label) = status_chip(status);
            html! {
                <div class="card">
                    <div class="result-head">
                        <span class={classes!("badge", cls)}>{ label }</span>
                        <span class="pill">{ format!("{pct}%") }</span>
                    </div>
                    <div class="bar"><div style={format!("width:{}%", pct.max(4))}></div></div>
                </div>
            }
        }
        Phase::Done(asset) => {
            let src = asset.path.clone().map(|p| format!("/{p}")).unwrap_or_default();
            html! {
                <div class="card">
                    <div class="result-head">
                        <span class="badge done">{ "Done" }</span>
                        <span class="pill">{ asset.id.clone() }</span>
                    </div>
                    <video src={src} controls=true autoplay=true muted=true loop=true />
                </div>
            }
        }
        Phase::Failed(e) => html! {
            <div class="card">
                <span class="badge failed">{ "Failed" }</span>
                <p class="err">{ e.clone() }</p>
            </div>
        },
    }
}

fn status_chip(s: &JobStatus) -> (&'static str, &'static str) {
    match s {
        JobStatus::Queued => ("queued", "Queued"),
        JobStatus::Processing => ("processing", "Generating"),
        JobStatus::Done => ("done", "Done"),
        JobStatus::Failed => ("failed", "Failed"),
    }
}

// ── API calls (all return the shared dto types) ──

async fn fetch_providers() -> Result<Vec<ModelInfo>, String> {
    let resp = Request::get("/api/providers").send().await.map_err(|e| e.to_string())?;
    let body: ProvidersResponse = resp.json().await.map_err(|e| e.to_string())?;
    Ok(body.models)
}

async fn submit_generate(req: &GenerateRequest) -> Result<GenerateResponse, String> {
    let resp = Request::post("/api/jobs/generate")
        .json(req).map_err(|e| e.to_string())?
        .send().await.map_err(|e| e.to_string())?;
    if !resp.ok() {
        let txt = resp.text().await.unwrap_or_default();
        return Err(extract_err(&txt, resp.status()));
    }
    resp.json().await.map_err(|e| e.to_string())
}

async fn poll_until_done(job_id: String, phase: UseStateHandle<Phase>) {
    loop {
        TimeoutFuture::new(1200).await;
        let job = match Request::get(&format!("/api/jobs/{job_id}")).send().await {
            Ok(r) => match r.json::<JobResponse>().await {
                Ok(j) => j.job,
                Err(e) => { phase.set(Phase::Failed(format!("poll decode: {e}"))); return; }
            },
            Err(e) => { phase.set(Phase::Failed(format!("poll: {e}"))); return; }
        };
        match job.status {
            JobStatus::Done => {
                match fetch_first_generated().await {
                    Ok(Some(asset)) => phase.set(Phase::Done(asset)),
                    Ok(None) => phase.set(Phase::Failed("job done but no asset found".into())),
                    Err(e) => phase.set(Phase::Failed(e)),
                }
                return;
            }
            JobStatus::Failed => {
                phase.set(Phase::Failed(job.error.unwrap_or_else(|| "generation failed".into())));
                return;
            }
            other => phase.set(Phase::Running { progress: job.progress, status: other }),
        }
    }
}

async fn fetch_first_generated() -> Result<Option<Asset>, String> {
    let url = format!("/api/assets?project={PROJECT}&kind=generated");
    let resp = Request::get(&url).send().await.map_err(|e| e.to_string())?;
    let body: AssetsResponse = resp.json().await.map_err(|e| e.to_string())?;
    // Newest first (the API orders by created_at desc), so the head is latest.
    Ok(body.assets.into_iter().next())
}

/// Pull the human message out of the API's {"error": "..."} body if present.
fn extract_err(body: &str, status: u16) -> String {
    serde_json::from_str::<serde_json::Value>(body)
        .ok()
        .and_then(|v| v.get("error").and_then(|e| e.as_str()).map(String::from))
        .unwrap_or_else(|| format!("HTTP {status}"))
}

/// Minimal base64 encoder (avoids pulling the base64 crate for one use).
fn base64_encode(data: &[u8]) -> String {
    const T: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut out = String::with_capacity((data.len() + 2) / 3 * 4);
    for chunk in data.chunks(3) {
        let b = [chunk[0], *chunk.get(1).unwrap_or(&0), *chunk.get(2).unwrap_or(&0)];
        let n = (b[0] as u32) << 16 | (b[1] as u32) << 8 | b[2] as u32;
        out.push(T[(n >> 18 & 63) as usize] as char);
        out.push(T[(n >> 12 & 63) as usize] as char);
        out.push(if chunk.len() > 1 { T[(n >> 6 & 63) as usize] as char } else { '=' });
        out.push(if chunk.len() > 2 { T[(n & 63) as usize] as char } else { '=' });
    }
    out
}

fn main() {
    yew::Renderer::<App>::new().render();
}
