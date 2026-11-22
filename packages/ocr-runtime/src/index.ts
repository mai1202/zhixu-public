import type { OcrItem, OcrResult } from '@notification-management/contracts';

export type { OcrItem, OcrResult } from '@notification-management/contracts';

export interface OcrProgress {
  phase: string;
  percent?: number;
  indeterminate?: boolean;
}

export type OcrProgressHandler = (detail: OcrProgress) => void;
export type OcrModel = 'tiny' | 'small';

interface OcrRuntimeModule {
  recognizeImage(file: File, model: OcrModel, executionLocation: OcrResult['execution_location'], onProgress?: OcrProgressHandler): Promise<OcrResult>;
}

let runtimePromise: Promise<OcrRuntimeModule> | undefined;

/** 浏览器与服务端 runner 都动态加载同一份固定版 OCR bundle。 */
export async function recognizeImage(
  file: File,
  model: OcrModel = 'tiny',
  executionLocation: OcrResult['execution_location'] = 'browser',
  onProgress?: OcrProgressHandler,
): Promise<OcrResult> {
  runtimePromise ??= import(/* @vite-ignore */ new URL('/ocr-runtime/runtime.js', window.location.origin).href)
    .then((runtime) => runtime as OcrRuntimeModule)
    .catch((error: unknown) => {
      runtimePromise = undefined;
      throw error;
    });
  const runtime = await runtimePromise;
  return runtime.recognizeImage(file, model, executionLocation, onProgress);
}
