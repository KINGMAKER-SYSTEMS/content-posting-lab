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
  group: string;
  key_id: string;
  pricing: string;
  models: string[];
}

// Provider schema types (from /api/video/provider-schemas)
export interface SchemaField {
  type: 'select' | 'range' | 'toggle' | 'text';
  label: string;
  default: string | number | boolean;
  options?: (string | number)[];
  min?: number;
  max?: number;
  step?: number;
  note?: string;
  placeholder?: string;
}

export type ProviderSchema = {
  [key: string]: SchemaField | Record<string, SchemaField> | boolean | undefined;
  _advanced?: Record<string, SchemaField>;
  image_required?: boolean;
};

export type ProviderSchemas = Record<string, ProviderSchema>;

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

export interface CropEntry {
  file: string;
  url: string;
}

export interface VideoEntry {
  index: number;
  status: "queued" | "generating" | "polling" | "downloading" | "processing" | "done" | "failed" | "error";
  file?: string;
  url?: string;
  error?: string;
  crops?: CropEntry[];
}

export interface Job {
  id: string;
  prompt: string;
  provider: string;
  count: number;
  project?: string;
  label?: string;
  videos: VideoEntry[];
}

export interface ErrorResponse {
  error: string;
  detail?: string;
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

export type MoodTag =
  | "sad"
  | "hype"
  | "love"
  | "funny"
  | "chill";

export const MOOD_TAGS: MoodTag[] = [
  "sad",
  "hype",
  "love",
  "funny",
  "chill",
];

export const MOOD_COLORS: Record<MoodTag, string> = {
  sad: "bg-blue-100 text-blue-800 border-blue-300",
  hype: "bg-red-100 text-red-800 border-red-300",
  love: "bg-pink-100 text-pink-800 border-pink-300",
  funny: "bg-yellow-100 text-yellow-800 border-yellow-300",
  chill: "bg-teal-100 text-teal-800 border-teal-300",
};

export interface CaptionResult {
  index: number;
  video_id: string;
  video_url: string;
  caption: string;
  mood?: MoodTag | null;
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
  | { event: "ocr_done"; index: number; total: number; video_id: string; caption: string; mood?: MoodTag | null; error?: string }
  | { event: "all_complete"; results: CaptionResult[]; csv: string | null; username: string }
  | { event: "error"; error: string };

export interface CaptionWSStartMessage {
  action: "start";
  profile_url: string;
  max_videos: number;
  sort: "latest" | "popular";
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

export interface ColorCorrection {
  brightness: number;
  contrast: number;
  saturation: number;
  sharpness: number;
  shadow: number;
  temperature: number;
  tint: number;
  fade: number;
}

export interface BurnPair {
  videoPath: string;
  overlayPng?: string;
  colorCorrection?: ColorCorrection | null;
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
  label?: string;
  count: number;
  created: number;
}

export interface VideosResponse {
  videos: VideoFile[];
}

export interface CaptionsResponse {
  sources: CaptionSource[];
}

export interface CaptionEntry {
  text: string;
  mood: MoodTag | null;
}

export interface CaptionCategory {
  id: string;
  name: string;
  captions: CaptionEntry[];
  count: number;
}

export interface CaptionBankResponse {
  categories: CaptionCategory[];
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
  colorCorrection?: ColorCorrection | null;
}

// ============================================================================
// HEALTH CHECK (all servers)
// ============================================================================

export interface HealthResponse {
  status: "ok" | "degraded";
  ffmpeg: boolean;
  ytdlp: boolean;
  providers: Record<string, boolean>;
  postiz?: boolean;
}

// ============================================================================
// POSTIZ PUBLISHING
// ============================================================================

export interface PostizIntegration {
  id: string;
  name: string;
  picture?: string;
  provider: string;  // e.g. "tiktok", "instagram", etc.
  disabled?: boolean;
}

export interface PostizStatusResponse {
  configured: boolean;
  reachable: boolean;
}

export interface PublishableVideo {
  name: string;
  path: string;  // relative path for static serving
  size: number;
}

export interface PublishableBatch {
  batch_id: string;
  created: number;
  videos: PublishableVideo[];
}

export interface PublishableVideosResponse {
  batches: PublishableBatch[];
}

export interface PostizUploadResponse {
  path: string;
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

// ============================================================================
// SLIDESHOW
// ============================================================================

export interface SlideshowImage {
  name: string;
  path: string;
}

export interface SlideshowImagesResponse {
  images: SlideshowImage[];
}

export interface SlideConfig {
  image: string;
  duration: number;
}

export interface SlideshowRenderRequest {
  project: string;
  slides: SlideConfig[];
  fps?: number;
}

export interface SlideshowRenderJob {
  status: 'pending' | 'running' | 'complete' | 'error' | 'not_found';
  progress: number;
  message: string;
  output?: string;
  path?: string;
}

export interface SlideshowRender {
  name: string;
  path: string;
}

export interface SlideshowRendersResponse {
  renders: SlideshowRender[];
}

// ── Roster ────────────────────────────────────────────────────────────────

export interface RosterPage {
  integration_id: string;
  name: string;
  provider: string;
  picture?: string;
  project: string | null;
  drive_folder_url: string | null;
  drive_folder_id: string | null;
  added_at: string;
  updated_at: string;
}

export interface RosterResponse {
  pages: RosterPage[];
}

export interface RosterSyncResponse {
  added: number;
  removed: number;
  pages: RosterPage[];
}

// ── Telegram ─────────────────────────────────────────────────────────────

export interface TelegramStatus {
  bot_configured: boolean;
  bot_running: boolean;
  bot_username: string | null;
  staging_group: TelegramStagingGroup | null;
  poster_count: number;
  total_inventory: number;
  schedule: TelegramSchedule;
}

export interface TelegramStagingGroup {
  chat_id: number | null;
  name: string | null;
  topic_count: number;
  topics: Record<string, TelegramTopicInfo>;
}

export interface TelegramTopicInfo {
  topic_id: number;
  topic_name: string;
  inventory_total?: number;
  inventory_pending?: number;
  inventory_forwarded?: number;
}

export interface TelegramPoster {
  poster_id: string;
  name: string;
  chat_id: number;
  page_ids: string[];
  topics: Record<string, { topic_id: number; topic_name: string }>;
  added_at: string;
  updated_at: string;
}

export interface TelegramInventoryItem {
  id: string;
  message_id: number;
  media_type: string;
  file_name: string;
  file_id: string;
  caption: string | null;
  source: 'api' | 'manual';
  added_at: string;
  forwarded: Record<string, { poster_id: string; message_id: number; forwarded_at: string }>;
}

export interface TelegramInventorySummary {
  integration_id: string;
  page_name: string;
  total: number;
  pending: number;
  forwarded: number;
}

export interface TelegramSound {
  id: string;
  url: string;
  label: string;
  added_at: string;
  active: boolean;
}

export interface TelegramSchedule {
  enabled: boolean;
  forward_time: string;
  timezone: string;
  last_run: string | null;
}

export interface TelegramBatchResult {
  posters_notified: number;
  videos_forwarded: number;
  sounds_sent: number;
}
