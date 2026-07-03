//! Leptos spike — reproduces the Content Lab **Home** dashboard (frontend/src/
//! pages/Home.tsx) in Leptos/WASM, using the real brand tokens (see index.html).
//!
//! Purpose: evidence for "could we do the whole frontend in Leptos and make it
//! look EXACTLY the same?" — see FINDINGS.md for the verdict. Mock data stands in
//! for the /api/projects fetch so the spike is self-contained (the real port
//! would use leptos `Resource` + gloo-net, both already compiled in — proven).

use leptos::prelude::*;

#[derive(Clone)]
struct Project {
    name: String,
    videos: u32,
    captions: u32,
    burned: u32,
}

fn seed() -> Vec<Project> {
    vec![
        Project { name: "Drake — Release Push".into(), videos: 12, captions: 48, burned: 9 },
        Project { name: "In Color — Summer".into(), videos: 6, captions: 22, burned: 4 },
        Project { name: "Alex Nicol — Debut".into(), videos: 3, captions: 11, burned: 0 },
        Project { name: "Gregory Alan Isakov".into(), videos: 8, captions: 30, burned: 6 },
    ]
}

fn main() {
    console_error_panic_hook::set_once();
    leptos::mount::mount_to_body(App);
}

#[component]
fn App() -> impl IntoView {
    let (projects, set_projects) = signal(seed());
    let (active, set_active) = signal(Some("Drake — Release Push".to_string()));

    // Derived totals (Home.tsx's `totals` useMemo → a Leptos derived signal).
    let totals = move || {
        projects.get().iter().fold((0u32, 0u32, 0u32), |a, p| {
            (a.0 + p.videos, a.1 + p.captions, a.2 + p.burned)
        })
    };

    let add = move |_| {
        set_projects.update(|ps| {
            let n = ps.len() + 1;
            ps.insert(0, Project { name: format!("New Project {n}"), videos: 0, captions: 0, burned: 0 });
        });
    };

    view! {
        <div class="wrap">
            <div class="row between">
                <div>
                    <div class="eyebrow">"Rising Tides · Content Lab"</div>
                    <h1>"Welcome back, " <span class="grad">"Smaths"</span></h1>
                    <div class="sub">"Generate, clip, burn, and distribute — all from one place."</div>
                </div>
                <button class="btn" on:click=add>"+ New Project"</button>
            </div>

            <div class="stats">
                <Stat label="Videos" value=Signal::derive(move || totals().0) />
                <Stat label="Captions" value=Signal::derive(move || totals().1) />
                <Stat label="Burned" value=Signal::derive(move || totals().2) />
                <Stat label="Projects" value=Signal::derive(move || projects.get().len() as u32) />
            </div>

            <div class="section-title">"Pipeline status"</div>
            <div class="pipeline">
                <Pill kind="info" text="2 videos generating (grok, wan-t2v)" />
                <Pill kind="warn" text="9 videos ready to burn" />
                <Pill kind="info" text="Upload queue: 3 pending" />
            </div>

            <div class="section-title">"Projects"</div>
            <div class="grid">
                <For
                    each=move || projects.get()
                    key=|p| p.name.clone()
                    let:p
                >
                    {
                        let name = p.name.clone();
                        let name_for_click = name.clone();
                        let name_for_del = name.clone();
                        let is_active = move || active.get().as_deref() == Some(name.as_str());
                        view! {
                            <div
                                class="card"
                                class:active=is_active
                                on:click=move |_| set_active.set(Some(name_for_click.clone()))
                            >
                                <div class="row between">
                                    <h3>{p.name.clone()}</h3>
                                    <button
                                        class="x"
                                        on:click=move |ev| {
                                            ev.stop_propagation();
                                            let target = name_for_del.clone();
                                            set_projects.update(|ps| ps.retain(|x| x.name != target));
                                        }
                                    >"✕"</button>
                                </div>
                                <div class="counts">
                                    <div class="count"><div class="n">{p.videos}</div><div class="l">"Videos"</div></div>
                                    <div class="count"><div class="n">{p.captions}</div><div class="l">"Captions"</div></div>
                                    <div class="count"><div class="n">{p.burned}</div><div class="l">"Burned"</div></div>
                                </div>
                            </div>
                        }
                    }
                </For>
            </div>
        </div>
    }
}

#[component]
fn Stat(label: &'static str, value: Signal<u32>) -> impl IntoView {
    view! {
        <div class="stat">
            <div class="n">{move || value.get()}</div>
            <div class="l">{label}</div>
        </div>
    }
}

#[component]
fn Pill(kind: &'static str, text: &'static str) -> impl IntoView {
    let badge_class = format!("badge {kind}");
    view! {
        <div class="pill-row">
            <span class=badge_class><span class="dot"></span>{text}</span>
        </div>
    }
}
