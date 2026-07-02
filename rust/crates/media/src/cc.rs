//! Color-correction filter builder — a verbatim port of the old
//! `build_cc_filter` (services/ffmpeg.py). Pure arithmetic composing a 3x3
//! color matrix + offset into a single ffmpeg `colorchannelmixer`, plus
//! optional unsharp. Ported exactly because small numeric drift visibly
//! changes the output (the sliders are user-calibrated).

use serde::Deserialize;

/// The 8 color-correction sliders. Each defaults to 0 (no-op).
#[derive(Debug, Clone, Default, Deserialize)]
pub struct ColorCorrection {
    #[serde(default)]
    pub brightness: f64,
    #[serde(default)]
    pub contrast: f64,
    #[serde(default)]
    pub saturation: f64,
    #[serde(default)]
    pub sharpness: f64,
    #[serde(default)]
    pub shadow: f64,
    #[serde(default)]
    pub temperature: f64,
    #[serde(default)]
    pub tint: f64,
    #[serde(default)]
    pub fade: f64,
}

impl ColorCorrection {
    fn is_default(&self) -> bool {
        [
            self.brightness,
            self.contrast,
            self.saturation,
            self.sharpness,
            self.shadow,
            self.temperature,
            self.tint,
            self.fade,
        ]
        .iter()
        .all(|v| *v == 0.0)
    }
}

type Mat = [[f64; 3]; 3];
type Vec3 = [f64; 3];

fn mat_mul(a: &Mat, b: &Mat) -> Mat {
    let mut out = [[0.0; 3]; 3];
    for i in 0..3 {
        for j in 0..3 {
            out[i][j] = (0..3).map(|k| a[i][k] * b[k][j]).sum();
        }
    }
    out
}

fn mat_vec(m: &Mat, v: &Vec3) -> Vec3 {
    let mut out = [0.0; 3];
    for i in 0..3 {
        out[i] = (0..3).map(|j| m[i][j] * v[j]).sum();
    }
    out
}

/// Build the `-vf` filter string. `scale` = Some("1080:1920") appends a lanczos
/// scale + setsar. Returns "null" (ffmpeg no-op) when there's nothing to do.
pub fn build_cc_filter(cc: Option<&ColorCorrection>, scale: Option<&str>) -> String {
    let scale_filter = scale.map(|s| format!("scale={s}:flags=lanczos,setsar=1"));

    let cc = match cc {
        Some(c) if !c.is_default() => c,
        _ => return scale_filter.unwrap_or_else(|| "null".to_string()),
    };

    let mut css_brightness = 1.0 + cc.brightness / 100.0;
    let mut css_contrast = 1.0 + cc.contrast / 100.0;
    let mut css_saturate = 1.0 + cc.saturation / 100.0;

    if cc.fade > 0.0 {
        let fade = cc.fade / 100.0;
        css_brightness = (css_brightness + fade * 0.4).min(2.0);
        css_contrast = (css_contrast - fade * 0.3).max(0.2);
        css_saturate = (css_saturate - fade * 0.4).max(0.2);
    }
    if cc.shadow != 0.0 {
        css_brightness += cc.shadow / 400.0;
    }

    let sharpness = cc.sharpness / 50.0;

    // Re-check for an effective no-op after the CSS-equivalent transforms.
    let effective_default = (css_brightness - 1.0).abs() < 0.005
        && (css_contrast - 1.0).abs() < 0.005
        && (css_saturate - 1.0).abs() < 0.005
        && cc.temperature.abs() <= 1.0
        && cc.tint.abs() <= 1.0
        && sharpness < 0.001;
    if effective_default {
        return scale_filter.unwrap_or_else(|| "null".to_string());
    }

    let mut mat: Mat = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]];
    let mut off: Vec3 = [0.0, 0.0, 0.0];

    if (css_brightness - 1.0).abs() >= 0.005 {
        let b = css_brightness;
        for row in mat.iter_mut() {
            for v in row.iter_mut() {
                *v *= b;
            }
        }
        off = [b * off[0], b * off[1], b * off[2]];
    }

    if (css_contrast - 1.0).abs() >= 0.005 {
        let c = css_contrast;
        let bias = 0.5 * (1.0 - c);
        for row in mat.iter_mut() {
            for v in row.iter_mut() {
                *v *= c;
            }
        }
        off = [c * off[0] + bias, c * off[1] + bias, c * off[2] + bias];
    }

    if (css_saturate - 1.0).abs() >= 0.005 {
        let s = css_saturate;
        let (sr, sg, sb) = (0.2126, 0.7152, 0.0722);
        let sat: Mat = [
            [sr + (1.0 - sr) * s, sg - sg * s, sb - sb * s],
            [sr - sr * s, sg + (1.0 - sg) * s, sb - sb * s],
            [sr - sr * s, sg - sg * s, sb + (1.0 - sb) * s],
        ];
        off = mat_vec(&sat, &off);
        mat = mat_mul(&sat, &mat);
    }

    if cc.temperature.abs() > 1.0 {
        let t_mat: Mat = if cc.temperature > 0.0 {
            let amt = (cc.temperature / 200.0).min(1.0);
            [
                [1.0 - amt + amt * 0.393, amt * 0.769, amt * 0.189],
                [amt * 0.349, 1.0 - amt + amt * 0.686, amt * 0.168],
                [amt * 0.272, amt * 0.534, 1.0 - amt + amt * 0.131],
            ]
        } else {
            let rad = (cc.temperature / 5.0).to_radians();
            hue_rotate_matrix(rad)
        };
        off = mat_vec(&t_mat, &off);
        mat = mat_mul(&t_mat, &mat);
    }

    if cc.tint.abs() > 1.0 {
        let rad = (cc.tint / 3.0).to_radians();
        let ti_mat = hue_rotate_matrix(rad);
        off = mat_vec(&ti_mat, &off);
        mat = mat_mul(&ti_mat, &mat);
    }

    let ccm = format!(
        "colorchannelmixer=\
         rr={:.6}:rg={:.6}:rb={:.6}:ra={:.6}:\
         gr={:.6}:gg={:.6}:gb={:.6}:ga={:.6}:\
         br={:.6}:bg={:.6}:bb={:.6}:ba={:.6}",
        mat[0][0], mat[0][1], mat[0][2], off[0],
        mat[1][0], mat[1][1], mat[1][2], off[1],
        mat[2][0], mat[2][1], mat[2][2], off[2],
    );

    let mut filters = vec!["format=rgb24".to_string(), ccm];
    if sharpness >= 0.001 {
        filters.push(format!("unsharp=5:5:{sharpness:.2}:5:5:{sharpness:.2}"));
    }
    if let Some(sf) = scale_filter {
        filters.push(sf);
    }
    filters.join(",")
}

/// The standard luminance-preserving hue-rotation matrix used for both the
/// cool-temperature and tint paths in the original.
fn hue_rotate_matrix(rad: f64) -> Mat {
    let (cos_a, sin_a) = (rad.cos(), rad.sin());
    [
        [
            0.213 + 0.787 * cos_a - 0.213 * sin_a,
            0.715 - 0.715 * cos_a - 0.715 * sin_a,
            0.072 - 0.072 * cos_a + 0.928 * sin_a,
        ],
        [
            0.213 - 0.213 * cos_a + 0.143 * sin_a,
            0.715 + 0.285 * cos_a + 0.140 * sin_a,
            0.072 - 0.072 * cos_a - 0.283 * sin_a,
        ],
        [
            0.213 - 0.213 * cos_a - 0.787 * sin_a,
            0.715 - 0.715 * cos_a + 0.715 * sin_a,
            0.072 + 0.928 * cos_a + 0.072 * sin_a,
        ],
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_is_null() {
        assert_eq!(build_cc_filter(None, None), "null");
        assert_eq!(build_cc_filter(Some(&ColorCorrection::default()), None), "null");
    }

    #[test]
    fn default_with_scale() {
        assert_eq!(
            build_cc_filter(None, Some("1080:1920")),
            "scale=1080:1920:flags=lanczos,setsar=1"
        );
    }

    #[test]
    fn brightness_produces_mixer() {
        let cc = ColorCorrection {
            brightness: 20.0,
            ..Default::default()
        };
        let f = build_cc_filter(Some(&cc), None);
        assert!(f.contains("colorchannelmixer"));
        assert!(f.starts_with("format=rgb24,"));
    }
}
