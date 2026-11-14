import Foundation
import ImageIO
import Vision

struct Block: Encodable {
    let text: String
    let confidence: Float
    let box: [Double]
}

struct Result: Encodable {
    let width: Int
    let height: Int
    let blocks: [Block]
}

struct OCRError: Encodable, Error {
    let error: String
}

func recognize(_ data: Data) throws -> Result {
    guard let source = CGImageSourceCreateWithData(data as CFData, nil),
          let type = CGImageSourceGetType(source),
          ["public.jpeg", "public.png"].contains(type as String),
          let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
          let width = properties[kCGImagePropertyPixelWidth] as? Int,
          let height = properties[kCGImagePropertyPixelHeight] as? Int,
          width > 0, height > 0 else {
        throw OCRError(error: "无法读取图片，请上传有效的 JPEG 或 PNG。")
    }
    guard width <= 12_000, height <= 12_000, width * height <= 60_000_000 else {
        throw OCRError(error: "图片像素过大，请缩小到 6000 万像素以内后重试。")
    }
    guard let image = CGImageSourceCreateImageAtIndex(source, 0, nil),
          CGImageSourceGetStatusAtIndex(source, 0) == .statusComplete else {
        throw OCRError(error: "图片未能完整解码，请重新导出后上传。")
    }

    let orientationValue = properties[kCGImagePropertyOrientation] as? UInt32 ?? 1
    let orientation = CGImagePropertyOrientation(rawValue: orientationValue) ?? .up
    let swapsAxes = [.left, .leftMirrored, .right, .rightMirrored].contains(orientation)
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = .accurate
    request.recognitionLanguages = ["zh-Hans", "en-US"]

    do {
        try VNImageRequestHandler(cgImage: image, orientation: orientation, options: [:]).perform([request])
    } catch {
        throw OCRError(error: "系统文字识别未能完成，请检查图片后重试。")
    }

    let blocks = (request.results ?? []).compactMap { observation -> Block? in
        guard let candidate = observation.topCandidates(1).first else { return nil }
        let rect = observation.boundingBox
        let x = min(1, max(0, rect.minX))
        let y = min(1, max(0, 1 - rect.maxY))
        let right = min(1, max(x, rect.maxX))
        let bottom = min(1, max(y, 1 - rect.minY))
        return Block(text: candidate.string, confidence: candidate.confidence,
                     box: [Double(x), Double(y), Double(right - x), Double(bottom - y)])
    }
    return Result(width: swapsAxes ? image.height : image.width,
                  height: swapsAxes ? image.width : image.height,
                  blocks: blocks)
}

let encoder = JSONEncoder()
do {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    let result = try recognize(data)
    FileHandle.standardOutput.write(try encoder.encode(result))
} catch let error as OCRError {
    FileHandle.standardOutput.write(try encoder.encode(error))
    exit(2)
} catch {
    FileHandle.standardOutput.write(try encoder.encode(OCRError(error: "图片识别失败，请重新上传后重试。")))
    exit(2)
}
