export interface OcrItem {
  id: string;
  text: string;
  poly: [[number, number], [number, number], [number, number], [number, number]];
  score: number | null;
}

export interface OcrResult {
  engine_version: string;
  model_id: string;
  execution_location: 'browser' | 'server';
  image_hash: string;
  width: number;
  height: number;
  orientation: 0 | 90 | 180 | 270;
  elapsed_ms: number;
  items: OcrItem[];
  warnings: string[];
}
