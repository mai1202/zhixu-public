//#region packages/ocr-runtime/src/runtime.ts
var e = new URL("/ocr-runtime/", window.location.origin), t = "paddleocr-js-0.4.2", n = /* @__PURE__ */ new Map(), r = /* @__PURE__ */ new Map();
function i(e, t, n, r = !1) {
	e?.({
		phase: t,
		...n === void 0 ? {} : { percent: n },
		...r ? { indeterminate: !0 } : {}
	});
}
function a(e) {
	if (e.length < 4 || e[0] !== 255 || e[1] !== 216) return 0;
	let t = new DataView(e.buffer, e.byteOffset, e.byteLength), n = 2;
	for (; n + 4 <= e.length && e[n] === 255;) {
		let r = e[n + 1], i = t.getUint16(n + 2);
		if (i < 2 || n + 2 + i > e.length) break;
		if (r === 225 && i >= 10 && String.fromCharCode(...e.subarray(n + 4, n + 10)) === "Exif\0\0") {
			let r = n + 10, i = e[r] === 73 && e[r + 1] === 73;
			if (!i && (e[r] !== 77 || e[r + 1] !== 77)) return 0;
			let a = (e) => t.getUint16(e, i), o = (e) => t.getUint32(e, i);
			if (a(r + 2) !== 42) return 0;
			let s = r + o(r + 4);
			if (s + 2 > e.length) return 0;
			let c = a(s);
			for (let t = 0; t < c; t += 1) {
				let n = s + 2 + t * 12;
				if (n + 12 > e.length) break;
				if (a(n) === 274 && a(n + 2) === 3 && o(n + 4) >= 1) {
					let e = a(n + 8);
					return e === 3 ? 180 : e === 6 ? 90 : e === 8 ? 270 : 0;
				}
			}
			return 0;
		}
		n += 2 + i;
	}
	return 0;
}
async function o(t, n) {
	let r = new URL("vendor/paddleocr/paddleocr.esm.js", e).href, a = new URL("vendor/paddleocr/assets/worker-entry-C9UNuyOJ.js", e), o = new URL("vendor/onnxruntime/", e).href, c = t === "small" ? "small" : "tiny", l = new URL("vendor/paddleocr/models/", e);
	await s(t, c, n), i(n, "正在初始化本地 OCR 引擎", 5);
	let u = await (await import(
		/* @vite-ignore */
		r
)).PaddleOCR.create({
		textDetectionModelName: `PP-OCRv6_${c}_det`,
		textRecognitionModelName: `PP-OCRv6_${c}_rec`,
		textDetectionModelAsset: { url: new URL(`PP-OCRv6_${c}_det_onnx_infer.tar`, l).href },
		textRecognitionModelAsset: { url: new URL(`PP-OCRv6_${c}_rec_onnx_infer.tar`, l).href },
		worker: { createWorker: () => new Worker(a, { type: "module" }) },
		ortOptions: {
			backend: "wasm",
			wasmPaths: o,
			simd: !0,
			numThreads: 1
		}
	});
	return i(n, "OCR 模型已就绪", 100), u;
}
async function s(t, n, a) {
	let o = r.get(t);
	o || (o = (async () => {
		let t = await fetch(new URL("resource-manifest.json", e), { cache: "force-cache" });
		if (!t.ok) throw Error("OCR 资源清单加载失败。");
		let r = ((await t.json()).files ?? []).filter(({ path: e }) => !e.includes("/models/") || e.includes(`_${n}_`)), o = r.reduce((e, t) => e + t.bytes, 0), s = 0;
		for (let t of r) {
			let n = await fetch(new URL(t.path, e), { cache: "force-cache" });
			if (!n.ok) throw Error(`OCR 本地资源加载失败：${t.path}`);
			if (n.body) {
				let e = n.body.getReader();
				for (;;) {
					let { done: t, value: n } = await e.read();
					if (t) break;
					s += n.byteLength, i(a, "正在准备本地 OCR 模型与运行环境", Math.min(100, Math.round(s * 100 / o)));
				}
			} else s += (await n.arrayBuffer()).byteLength, i(a, "正在准备本地 OCR 模型与运行环境", Math.min(100, Math.round(s * 100 / o)));
		}
	})().catch((e) => {
		throw r.delete(t), e;
	}), r.set(t, o)), await o;
}
function c(e, t) {
	let r = n.get(e);
	return r || (r = o(e, t).catch((t) => {
		throw n.delete(e), t;
	}), n.set(e, r)), r;
}
function l(e, t) {
	let n = [];
	for (let [r, i] of e.entries()) {
		if (!Array.isArray(i.poly) || i.poly.length !== 4) {
			t.push(`第 ${r + 1} 个识别框的坐标格式无效，已跳过。`);
			continue;
		}
		let e = i.poly.map((e) => {
			if (!Array.isArray(e) || e.length < 2) return null;
			let t = Number(e[0]), n = Number(e[1]);
			return Number.isFinite(t) && Number.isFinite(n) && t >= 0 && n >= 0 ? [t, n] : null;
		});
		if (e.some((e) => e === null)) {
			t.push(`第 ${r + 1} 个识别框包含无效坐标，已跳过。`);
			continue;
		}
		let a = i.score === null || i.score === void 0 ? null : Number(i.score);
		n.push({
			id: `ocr-line-${r + 1}`,
			text: String(i.text ?? ""),
			poly: e,
			score: a !== null && Number.isFinite(a) ? Math.max(0, Math.min(1, a)) : null
		});
	}
	return n;
}
async function u(e, n, r, o) {
	if (e.size <= 0 || e.size > 20971520) throw Error("图片大小必须在 1 字节到 20 MiB 之间。");
	if (!["image/png", "image/jpeg"].includes(e.type)) throw Error("目前只接受 PNG 或 JPEG 图片。");
	let s = performance.now(), u = new Uint8Array(await e.arrayBuffer()), d = await crypto.subtle.digest("SHA-256", u), f = [...new Uint8Array(d)].map((e) => e.toString(16).padStart(2, "0")).join(""), p = e.type === "image/jpeg" ? a(u) : 0, m = await createImageBitmap(e, { imageOrientation: "from-image" }), { width: h, height: g } = m;
	if (m.close(), !h || !g || h * g > 25e6) throw Error("图片像素超过 2500 万，请先缩小图片后重试。");
	i(o, "图片检查完成，开始识别", 10);
	let _ = await c(n, o);
	i(o, "正在识别文字与定位框", void 0, !0);
	let [v] = await _.predict(e), y = [], b = l(v?.items ?? [], y);
	return i(o, "识别完成，可校正文字与图框", 100), {
		engine_version: t,
		model_id: `PP-OCRv6_${n}`,
		execution_location: r,
		image_hash: f,
		width: h,
		height: g,
		orientation: p,
		elapsed_ms: Math.max(0, Math.round(performance.now() - s)),
		items: b,
		warnings: y
	};
}
//#endregion
export { u as recognizeImage };
