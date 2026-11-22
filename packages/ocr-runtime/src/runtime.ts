import type { OcrItem, OcrModel, OcrProgress, OcrProgressHandler, OcrResult } from './index';

interface PaddleItem {
  text?: unknown;
  poly?: unknown;
  score?: unknown;
}

interface PaddleResult {
  items?: PaddleItem[];
}

interface PaddleEngine {
  predict(file: File): Promise<PaddleResult[]>;
}

interface PaddleOcrModule {
  PaddleOCR: {
    create(options: Record<string, unknown>): Promise<PaddleEngine>;
  };
}

const MAX_FILE_BYTES = 20 * 1024 * 1024;
const MAX_IMAGE_PIXELS = 25_000_000;
const OCR_BASE = new URL('/ocr-runtime/', window.location.origin);
const ENGINE_VERSION = 'paddleocr-js-0.4.2';
const engineCache = new Map<OcrModel, Promise<PaddleEngine>>();
const resourcePreparation = new Map<OcrModel, Promise<void>>();

function report(onProgress: OcrProgressHandler | undefined, phase: string, percent?: number, indeterminate = false): void {
  onProgress?.({ phase, ...(percent === undefined ? {} : { percent }), ...(indeterminate ? { indeterminate: true } : {}) });
}

function readJpegOrientation(bytes: Uint8Array): OcrResult['orientation'] {
  if (bytes.length < 4 || bytes[0] !== 0xff || bytes[1] !== 0xd8) return 0;
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  let offset = 2;
  while (offset + 4 <= bytes.length) {
    if (bytes[offset] !== 0xff) break;
    const marker = bytes[offset + 1];
    const size = view.getUint16(offset + 2);
    if (size < 2 || offset + 2 + size > bytes.length) break;
    if (marker === 0xe1 && size >= 10 && String.fromCharCode(...bytes.subarray(offset + 4, offset + 10)) === 'Exif\0\0') {
      const tiff = offset + 10;
      const littleEndian = bytes[tiff] === 0x49 && bytes[tiff + 1] === 0x49;
      if (!littleEndian && !(bytes[tiff] === 0x4d && bytes[tiff + 1] === 0x4d)) return 0;
      const read16 = (at: number) => view.getUint16(at, littleEndian);
      const read32 = (at: number) => view.getUint32(at, littleEndian);
      if (read16(tiff + 2) !== 42) return 0;
      const ifd = tiff + read32(tiff + 4);
      if (ifd + 2 > bytes.length) return 0;
      const count = read16(ifd);
      for (let index = 0; index < count; index += 1) {
        const entry = ifd + 2 + index * 12;
        if (entry + 12 > bytes.length) break;
        if (read16(entry) === 0x0112 && read16(entry + 2) === 3 && read32(entry + 4) >= 1) {
          const value = read16(entry + 8);
          if (value === 3) return 180;
          if (value === 6) return 90;
          if (value === 8) return 270;
          return 0;
        }
      }
      return 0;
    }
    offset += 2 + size;
  }
  return 0;
}

async function createEngine(model: OcrModel, onProgress?: OcrProgressHandler): Promise<PaddleEngine> {
  const moduleUrl = new URL('vendor/paddleocr/paddleocr.esm.js', OCR_BASE).href;
  const workerUrl = new URL('vendor/paddleocr/assets/worker-entry-C9UNuyOJ.js', OCR_BASE);
  const runtimeUrl = new URL('vendor/onnxruntime/', OCR_BASE).href;
  const variant = model === 'small' ? 'small' : 'tiny';
  const modelBase = new URL('vendor/paddleocr/models/', OCR_BASE);
  await prepareResources(model, variant, onProgress);
  report(onProgress, '正在初始化本地 OCR 引擎', 5);
  const sdk = await import(/* @vite-ignore */ moduleUrl) as PaddleOcrModule;
  const engine = await sdk.PaddleOCR.create({
    textDetectionModelName: `PP-OCRv6_${variant}_det`,
    textRecognitionModelName: `PP-OCRv6_${variant}_rec`,
    textDetectionModelAsset: {
      url: new URL(`PP-OCRv6_${variant}_det_onnx_infer.tar`, modelBase).href,
    },
    textRecognitionModelAsset: {
      url: new URL(`PP-OCRv6_${variant}_rec_onnx_infer.tar`, modelBase).href,
    },
    worker: { createWorker: () => new Worker(workerUrl, { type: 'module' }) },
    ortOptions: { backend: 'wasm', wasmPaths: runtimeUrl, simd: true, numThreads: 1 },
  });
  report(onProgress, 'OCR 模型已就绪', 100);
  return engine;
}

async function prepareResources(model: OcrModel, variant: string, onProgress?: OcrProgressHandler): Promise<void> {
  let pending = resourcePreparation.get(model);
  if (!pending) {
    pending = (async () => {
      const response = await fetch(new URL('resource-manifest.json', OCR_BASE), { cache: 'force-cache' });
      if (!response.ok) throw new Error('OCR 资源清单加载失败。');
      const manifest = await response.json() as { files?: Array<{ path: string; bytes: number }> };
      const selected = (manifest.files ?? []).filter(({ path }) => (
        !path.includes('/models/') || path.includes(`_${variant}_`)
      ));
      const totalBytes = selected.reduce((total, item) => total + item.bytes, 0);
      let loadedBytes = 0;
      for (const resource of selected) {
        const response = await fetch(new URL(resource.path, OCR_BASE), { cache: 'force-cache' });
        if (!response.ok) throw new Error(`OCR 本地资源加载失败：${resource.path}`);
        if (response.body) {
          const reader = response.body.getReader();
          while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            loadedBytes += value.byteLength;
            report(onProgress, '正在准备本地 OCR 模型与运行环境', Math.min(100, Math.round(loadedBytes * 100 / totalBytes)));
          }
        } else {
          loadedBytes += (await response.arrayBuffer()).byteLength;
          report(onProgress, '正在准备本地 OCR 模型与运行环境', Math.min(100, Math.round(loadedBytes * 100 / totalBytes)));
        }
      }
    })().catch((error: unknown) => {
      resourcePreparation.delete(model);
      throw error;
    });
    resourcePreparation.set(model, pending);
  }
  await pending;
}

function getEngine(model: OcrModel, onProgress?: OcrProgressHandler): Promise<PaddleEngine> {
  let pending = engineCache.get(model);
  if (!pending) {
    pending = createEngine(model, onProgress).catch((error: unknown) => {
      engineCache.delete(model);
      throw error;
    });
    engineCache.set(model, pending);
  }
  return pending;
}

function normalizeItems(items: PaddleItem[], warnings: string[]): OcrItem[] {
  const output: OcrItem[] = [];
  for (const [index, item] of items.entries()) {
    if (!Array.isArray(item.poly) || item.poly.length !== 4) {
      warnings.push(`第 ${index + 1} 个识别框的坐标格式无效，已跳过。`);
      continue;
    }
    const poly = item.poly.map((point: unknown) => {
      if (!Array.isArray(point) || point.length < 2) return null;
      const x = Number(point[0]);
      const y = Number(point[1]);
      return Number.isFinite(x) && Number.isFinite(y) && x >= 0 && y >= 0 ? [x, y] as [number, number] : null;
    });
    if (poly.some((point) => point === null)) {
      warnings.push(`第 ${index + 1} 个识别框包含无效坐标，已跳过。`);
      continue;
    }
    const scoreValue = item.score === null || item.score === undefined ? null : Number(item.score);
    output.push({
      id: `ocr-line-${index + 1}`,
      text: String(item.text ?? ''),
      poly: poly as OcrItem['poly'],
      score: scoreValue !== null && Number.isFinite(scoreValue) ? Math.max(0, Math.min(1, scoreValue)) : null,
    });
  }
  return output;
}

/** 浏览器和 Playwright runner 调用同一个识别入口与后处理。 */
export async function recognizeImage(
  file: File,
  model: OcrModel,
  executionLocation: OcrResult['execution_location'],
  onProgress?: OcrProgressHandler,
): Promise<OcrResult> {
  if (file.size <= 0 || file.size > MAX_FILE_BYTES) throw new Error('图片大小必须在 1 字节到 20 MiB 之间。');
  if (!['image/png', 'image/jpeg'].includes(file.type)) throw new Error('目前只接受 PNG 或 JPEG 图片。');
  const startedAt = performance.now();
  const bytes = new Uint8Array(await file.arrayBuffer());
  const hashBuffer = await crypto.subtle.digest('SHA-256', bytes);
  const imageHash = [...new Uint8Array(hashBuffer)].map((value) => value.toString(16).padStart(2, '0')).join('');
  const orientation = file.type === 'image/jpeg' ? readJpegOrientation(bytes) : 0;
  const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' });
  const { width, height } = bitmap;
  bitmap.close();
  if (!width || !height || width * height > MAX_IMAGE_PIXELS) throw new Error('图片像素超过 2500 万，请先缩小图片后重试。');
  report(onProgress, '图片检查完成，开始识别', 10);
  const engine = await getEngine(model, onProgress);
  report(onProgress, '正在识别文字与定位框', undefined, true);
  const [result] = await engine.predict(file);
  const warnings: string[] = [];
  const items = normalizeItems(result?.items ?? [], warnings);
  report(onProgress, '识别完成，可校正文字与图框', 100);
  return {
    engine_version: ENGINE_VERSION,
    model_id: `PP-OCRv6_${model}`,
    execution_location: executionLocation,
    image_hash: imageHash,
    width,
    height,
    orientation,
    elapsed_ms: Math.max(0, Math.round(performance.now() - startedAt)),
    items,
    warnings,
  };
}
