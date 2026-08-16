import Foundation
import Vision
import AppKit

// 批量识别多张图片的文字，输出 NDJSON：每张图片一行
// 用法：swift ocr_vision.swift --langs zh-Hans,en-US 图片1 图片2 ...

let args = CommandLine.arguments
var paths: [String] = []
var langs = ["zh-Hans", "en-US"]

var i = 1
while i < args.count {
    if args[i] == "--langs" {
        i += 1
        if i < args.count {
            langs = args[i].split(separator: ",").map { String($0) }
        }
    } else {
        paths.append(args[i])
    }
    i += 1
}

for path in paths {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
        print("{\"file\": \"\(path)\", \"lines\": [], \"error\": \"cannot open\"}")
        continue
    }

    var rawLines: [(text: String, x: Double, y: Double, w: Double, h: Double)] = []
    let req = VNRecognizeTextRequest { request, _ in
        guard let obs = request.results as? [VNRecognizedTextObservation] else { return }
        for o in obs {
            guard let t = o.topCandidates(1).first else { continue }
            let bb = o.boundingBox
            rawLines.append((
                text: t.string,
                x: Double(bb.minX),
                y: Double(1.0 - bb.maxY),
                w: Double(bb.width),
                h: Double(bb.height)
            ))
        }
    }
    req.recognitionLevel = .accurate
    req.recognitionLanguages = langs
    req.usesLanguageCorrection = true

    let handler = VNImageRequestHandler(cgImage: cg, options: [:])
    do {
        try handler.perform([req])
    } catch {
        fputs("Vision OCR 失败：\(path)：\(error)\n", stderr)
        exit(2)
    }

    rawLines.sort { $0.y < $1.y }
    var arr: [[String: Any]] = []
    for l in rawLines {
        arr.append(["text": l.text, "x": l.x, "y": l.y, "w": l.w, "h": l.h])
    }
    let obj: [String: Any] = ["file": path, "lines": arr]
    if let data = try? JSONSerialization.data(withJSONObject: obj),
       let s = String(data: data, encoding: .utf8) {
        print(s)
    }
}
