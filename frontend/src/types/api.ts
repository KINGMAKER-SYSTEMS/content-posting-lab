/**
 * TypeScript API contracts for all three servers
 * - Server 1 (port 8000): Video generation
 * - Server 2 (port 8001): Caption scraping
 * - Server 3 (port 8002): Caption burning
 */

// ============================================================================
// VIDEO GENERATION SERVER (port 8000)
// ============================================================================

export interface Provider {
  id: string;
  name: string;
  key_id: string;
  pricing: string;
  models: string[];
}

export interface GenerateRequest {
  prompt: string;
  provider: string;
  count: number;
  duration: number;
  aspect_ratio: string;
  resolution: string;
  media?: File;
}

export interface GenerateResponse {
  job_id: string;
  count: number;
}

export interface VideoEntry {
  index: number;
  status: "queued" | "processing" | "done" | "failed" | "error";
  file?: string;
  error?: string;
}

export interface Job {
  id: string;
  prompt: string;
  provider: string;
  count: number;
  project?: string;
  videos: VideoEntry[];
}

export interface ProvidersResponse {
  providers: Provider[];
}

export interface JobsListResponse {
  jobs: Job[];
}

// ============================================================================
// CAPTION SCRAPING SERVER (port 8001) - WebSocket Events
// ============================================================================

export interface CaptionResult {
  index: number;
  video_id: string;
  video_url: string;
  caption: string;
  error?: string;
}

export interface AllCompleteData {
  results: CaptionResult[];
  csv: string | null;
  username: string;
}

// WebSocket event discriminated union
export type CaptionWSMessage =
  | { event: "status"; text: string }
  | { event: "urls_collected"; count: number; urls: string[] }
  | { event: "downloading"; index: number; total: number; video_id: string }
  | { event: "frame_ready"; index: number; total: number; video_id: string; b64: string; video_url: string }
  | { event: "frame_error"; index: number; total: number; video_id: string; error: string }
  | { event: "ocr_starting"; total: number }
  | { event: "ocr_started"; index: number; total: number; video_id: string }
  | { event: "ocr_done"; index: number; total: number; video_id: string; caption: string; error?: string }
  | { event: "all_complete"; results: CaptionResult[]; csv: string | null; username: string }
  | { event: "error"; error: string };

export interface CaptionWSStartMessage {
  action: "start";
  profile_url: string;
  max_videos: number;
  sort: string;
  project?: string;
}

export interface ExportResponse {
  csv: string;
}

// ============================================================================
// CAPTION BURNING SERVER (port 8002)
// ============================================================================

export interface VideoFile {
  path: string;
  name: string;
  folder: string;
}

export interface CaptionRow {
  text: string;
  video_id: string;
  video_url: string;
}

export interface CaptionSource {
  username: string;
  csv_path: string;
  count: number;
  captions: CaptionRow[];
}

export interface FontInfo {
  file: string;
  name: string;
}

export interface BurnPair {
  videoPath: string;
  overlayPng?: string;
  colorCorrection?: string;
}

export interface BurnRequest {
  pairs: BurnPair[];
  position?: "top" | "center" | "bottom";
  fontSize?: number;
}

export interface BurnResult {
  index: number;
  ok: boolean;
  file?: string;
  error?: string;
}

export interface BurnResponse {
  index: number;
  ok: boolean;
  file?: string;
  error?: string;
}

export interface BurnBatch {
  id: string;
  count: number;
  created: number;
}

export interface VideosResponse {
  videos: VideoFile[];
}

export interface CaptionsResponse {
  sources: CaptionSource[];
}

export interface FontsResponse {
  fonts: FontInfo[];
}

export interface BatchesResponse {
  batches: BurnBatch[];
}

// WebSocket event for burn server (legacy)
export type BurnWSMessage =
  | { event: "burning"; index: number; total: number }
  | { event: "burned"; index: number; total: number; result: BurnResult }
  | { event: "complete"; batch_id: string; results: BurnResult[] };

export interface BurnOverlayRequest {
  batchId: string;
  index: number;
  videoPath: string;
  overlayPng?: string;
  colorCorrection?: string;
}

// ============================================================================
// HEALTH CHECK (all servers)
// ============================================================================

export interface HealthResponse {
  status: "ok" | "degraded";
  ffmpeg: boolean;
  ytdlp: boolean;
  providers: Record<string, boolean>;
}

// ============================================================================
// ============================================================================

export interface Project {
  name: string;
  path: string;
  video_count: number;
  caption_count: number;
  burned_count: number;
}

export interface CreateProjectRequest {
  name: string;
}

export interface ProjectListResponse {
  projects: Project[];
}

export interface CreateProjectResponse {
  project: Project;
}

export interface DeleteProjectResponse {
  deleted: boolean;
  name: string;
}
