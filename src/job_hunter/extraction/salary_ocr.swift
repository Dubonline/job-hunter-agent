// 本机薪资截图识别：仅使用 Apple Vision，不联网，不保存识别正文。
import Foundation
import Vision
import ImageIO
import CoreGraphics

// 参数1：临时薪资区域PNG路径；结果为候选文本和0到1的识别置信度。
guard [2, 6].contains(CommandLine.arguments.count),
      let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: CommandLine.arguments[1]) as CFURL, nil),
      let original = CGImageSourceCreateImageAtIndex(source, 0, nil) else { exit(2) }
// 可选参数2至5：DOM Range给出的月薪相对矩形；仅裁去薪数附注，不改变像素。
var input = original
if CommandLine.arguments.count == 6 {
    let values = CommandLine.arguments.dropFirst(2).compactMap(Double.init)
    guard values.count == 4, values.allSatisfy({ $0.isFinite && $0 >= 0 && $0 <= 1 }),
          values[2] > 0, values[3] > 0 else { exit(2) }
    let rect = CGRect(x: values[0] * Double(original.width), y: values[1] * Double(original.height),
                      width: values[2] * Double(original.width), height: values[3] * Double(original.height))
    guard let crop = original.cropping(to: rect.integral) else { exit(2) }
    input = crop
}
// 小字号先放大3倍，改善细横线与数字识别，不改变原图内容。
let width = input.width * 3, height = input.height * 3
let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                        bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
context.interpolationQuality = .high
context.draw(input, in: CGRect(x: 0, y: 0, width: width, height: height))
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["en-US"]
do {
    let enlarged = context.makeImage()!
    try VNImageRequestHandler(cgImage: enlarged, options: [:]).perform([request])
    let rows = (request.results ?? []).compactMap { observation -> [String: Any]? in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        return ["text": candidate.string, "confidence": candidate.confidence]
    }
    let data = try JSONSerialization.data(withJSONObject: rows)
    print(String(data: data, encoding: .utf8)!)
} catch { exit(3) }
