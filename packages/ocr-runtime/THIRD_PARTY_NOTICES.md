# OCR 第三方资源说明

## 固定资源

此目录按哈希复用了 `weight-tracker/static/vendor` 中的浏览器 OCR 运行资源，没有复制该项目的业务数据或配置。资源版本和 SHA-256 位于 [resource-manifest.json](public/ocr-runtime/resource-manifest.json)。

| 组件 | 版本/用途 | 许可证 | 随附文本 |
|---|---|---|---|
| PaddleOCR.js | 0.4.2 浏览器 SDK 与 PP-OCRv6 tiny/small 权重 | Apache-2.0 | [PADDLEOCR-LICENSE.txt](licenses/PADDLEOCR-LICENSE.txt) |
| OpenCV.js | 4.10.0-release.1 预处理 | Apache-2.0 | [OPENCV-JS-LICENSE.txt](licenses/OPENCV-JS-LICENSE.txt) |
| ONNX Runtime Web | 1.22.x WASM 推理 | MIT | [ONNX-RUNTIME-LICENSE.txt](licenses/ONNX-RUNTIME-LICENSE.txt) |
| clipper-lib | 6.4.2 多边形处理 | 项目包元数据声明 MIT | [上游仓库](https://github.com/junmer/clipper-lib) |
| js-yaml | 4.1.x 模型配置读取 | 项目包元数据声明 MIT | [上游仓库](https://github.com/nodeca/js-yaml) |

## 分发边界

原项目仅提供了构建后的 JS、WASM 和模型文件，没有随这些构建产物保存全部 npm 包许可证目录或 ONNX Runtime WASM 的完整第三方生成物清单。已随本项目保留上述主许可证和源包版本元数据；若将二进制 OCR 资源发布到公开下载渠道，应先按相同版本重建/取得上游分发包的完整第三方声明，并将其并入本目录。

上游资料：

- [PaddleOCR 与浏览器 SDK](https://github.com/PaddlePaddle/PaddleOCR/tree/main/paddleocr-js)
- [OpenCV.js](https://github.com/TechStark/opencv-js)
- [ONNX Runtime Web 1.22.0](https://github.com/microsoft/onnxruntime/tree/v1.22.0/js/web)
