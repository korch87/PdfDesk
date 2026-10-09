//  MacScanner.swift
//  PdfDesk macOS scanner helper (ImageCaptureCore, macOS 12+).
//
//  Commands:
//    MacScanner list [--timeout SECONDS]
//    MacScanner scan --device-id ID --dpi DPI --output PATH [--timeout SECONDS]
//
//  Contract with app/src/mac_scanner.py (keep both sides in sync):
//    * stdout carries exactly one JSON object, newline terminated;
//    * stderr carries a short human readable diagnostic only;
//    * the acquired image is written to the absolute --output path handed over by
//      the Python adapter (an app owned temporary file); nothing else is written;
//    * exit codes: 0 ok, 2 usage, 3 device not found, 4 timeout, 5 cancelled,
//      6 transfer failed, 7 scanner integration unavailable;
//    * discovery and transfer are asynchronous delegate callbacks driven by the
//      main run loop with an overall deadline;
//    * SIGINT/SIGTERM cancel the running scan and report error "cancelled";
//    * the helper never presents its own UI: the PdfDesk dialog owns the user
//      request and cancellation.
//
//  Build notes: app/native/BUILD-mac-scanner.md (swiftc only, no Xcode project).
//  Build status: this file has NOT been compiled or run on Mac hardware in the
//  current Windows development environment; see the build notes for validation
//  steps that must be performed on a real Mac.

import Foundation
import Dispatch
import ImageCaptureCore
import ImageIO
import CoreGraphics

// MARK: - Protocol constants

enum ExitCode {
    static let ok: Int32 = 0
    static let usage: Int32 = 2
    static let deviceNotFound: Int32 = 3
    static let timeout: Int32 = 4
    static let cancelled: Int32 = 5
    static let transferFailed: Int32 = 6
    static let unavailable: Int32 = 7
}

enum Limits {
    static let minDPI = 75
    static let maxDPI = 1200
    static let minTimeout = 5.0
    static let maxTimeout = 900.0
    static let maxDeviceIDLength = 1024
    static let helperVersion = "1.0"
    static let outputUTI = "public.jpeg"
    static let listingMinimumSeconds = 0.8
    static let listingSettleSeconds = 0.4
    static let listingFallbackSeconds = 3.0
    static let closeSessionGraceSeconds = 2.0
}

struct HelperFailure: Error {
    let code: String
    let message: String
    let exitCode: Int32
}

func helperFailure(_ code: String, _ message: String, exit exitCode: Int32) -> HelperFailure {
    return HelperFailure(code: code, message: message, exitCode: exitCode)
}

// MARK: - Structured stdout

func emitJSON(_ object: [String: Any]) {
    guard JSONSerialization.isValidJSONObject(object),
          let data = try? JSONSerialization.data(withJSONObject: object, options: []) else {
        return
    }
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data([0x0A]))
}

func reportAndExit(_ failure: HelperFailure) -> Never {
    emitJSON(["ok": false, "error": ["code": failure.code, "message": failure.message]])
    if let data = ("MacScanner: " + failure.message + "\n").data(using: .utf8) {
        FileHandle.standardError.write(data)
    }
    exit(failure.exitCode)
}

// MARK: - Command line

struct HelperOptions {
    var command: String = ""
    var deviceID: String = ""
    var dpi: Int = 300
    var outputPath: String = ""
    var timeout: Double = 120.0
    var verbose: Bool = false
}

func parseArguments(_ arguments: [String]) throws -> HelperOptions {
    var options = HelperOptions()
    var index = 0
    if let first = arguments.first, !first.hasPrefix("--") {
        options.command = first
        index = 1
    }
    while index < arguments.count {
        let key = arguments[index]
        func nextValue() throws -> String {
            guard index + 1 < arguments.count else {
                throw helperFailure("usage", "Не задано значение параметра \(key).", exit: ExitCode.usage)
            }
            index += 1
            return arguments[index]
        }
        switch key {
        case "--device-id":
            options.deviceID = try nextValue()
        case "--dpi":
            let raw = try nextValue()
            guard let parsed = Int(raw), parsed >= Limits.minDPI, parsed <= Limits.maxDPI else {
                throw helperFailure("usage",
                    "Разрешение должно быть целым числом от \(Limits.minDPI) до \(Limits.maxDPI) dpi.",
                    exit: ExitCode.usage)
            }
            options.dpi = parsed
        case "--output":
            options.outputPath = try nextValue()
        case "--timeout":
            let raw = try nextValue()
            guard let parsed = Double(raw), parsed.isFinite,
                  parsed >= Limits.minTimeout, parsed <= Limits.maxTimeout else {
                throw helperFailure("usage",
                    "Тайм-аут должен быть числом от \(Int(Limits.minTimeout)) до \(Int(Limits.maxTimeout)) секунд.",
                    exit: ExitCode.usage)
            }
            options.timeout = parsed
        case "--verbose":
            options.verbose = true
        default:
            throw helperFailure("usage", "Неизвестный параметр \(key).", exit: ExitCode.usage)
        }
        index += 1
    }
    switch options.command {
    case "list":
        break
    case "scan":
        if options.deviceID.isEmpty {
            throw helperFailure("usage", "Не задан идентификатор устройства сканирования.", exit: ExitCode.usage)
        }
        if options.deviceID.count > Limits.maxDeviceIDLength {
            throw helperFailure("usage", "Идентификатор устройства слишком длинный.", exit: ExitCode.usage)
        }
        if options.outputPath.isEmpty {
            throw helperFailure("usage", "Не задан путь для сохранения скана.", exit: ExitCode.usage)
        }
    default:
        throw helperFailure("usage", "Неизвестная команда. Используйте list или scan.", exit: ExitCode.usage)
    }
    return options
}

// MARK: - Helper

final class ScannerHelper: NSObject, ICDeviceBrowserDelegate, ICScannerDeviceDelegate {

    private enum Stage {
        case browsing
        case openingSession
        case selectingUnit
        case scanning
        case closingSession
    }

    private let options: HelperOptions
    private let outputURL: URL
    private let uploadDirectory: URL
    private let documentBaseName: String
    private let deadline: Date
    private let startTime = Date()

    private var browser: ICDeviceBrowser?
    private var scanners: [ICScannerDevice] = []
    private var stage: Stage = .browsing
    private var recentDeviceChange = Date.distantPast
    private var enumerationFinished = false
    private var cancelRequested = false
    private var signalSources: [DispatchSourceSignal] = []

    private var targetDevice: ICScannerDevice?
    private var pendingUnitTypes: [ICScannerFunctionalUnitType] = []
    private var scannedURL: URL?
    private var scanCompleted = false
    private var finalizing = false
    private var actualDPI = 0
    private var closeDeadline: Date?

    /// Validates the requested output path, then builds the helper state.
    static func make(options: HelperOptions) throws -> ScannerHelper {
        let candidate = URL(fileURLWithPath: options.outputPath, isDirectory: false).standardizedFileURL
        try validateOutput(candidate)
        return ScannerHelper(options: options, outputURL: candidate)
    }

    private init(options: HelperOptions, outputURL: URL) {
        self.options = options
        self.outputURL = outputURL
        self.uploadDirectory = outputURL.deletingLastPathComponent()
        self.documentBaseName = outputURL.deletingPathExtension().lastPathComponent
        self.deadline = Date().addingTimeInterval(options.timeout)
        super.init()
    }

    // MARK: Output path validation

    private static func validateOutput(_ url: URL) throws {
        guard url.path.hasPrefix("/") else {
            throw helperFailure("usage", "Путь сохранения скана должен быть абсолютным.",
                                exit: ExitCode.usage)
        }
        guard !url.pathComponents.contains("..") else {
            throw helperFailure("usage", "Путь сохранения скана содержит недопустимый переход каталога.",
                                exit: ExitCode.usage)
        }
        guard url.pathComponents.count > 1, !url.lastPathComponent.isEmpty,
              !url.lastPathComponent.hasPrefix(".") else {
            throw helperFailure("usage", "Недопустимое имя файла сканирования.", exit: ExitCode.usage)
        }
        let parent = url.deletingLastPathComponent()
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: parent.path, isDirectory: &isDirectory),
              isDirectory.boolValue else {
            throw helperFailure("write_failed", "Каталог для временного файла сканирования не найден.",
                                exit: ExitCode.transferFailed)
        }
        guard FileManager.default.isWritableFile(atPath: parent.path) else {
            throw helperFailure("write_failed", "Каталог для временного файла сканирования недоступен для записи.",
                                exit: ExitCode.transferFailed)
        }
        if let attributes = try? FileManager.default.attributesOfItem(atPath: url.path) {
            let kind = attributes[.type] as? FileAttributeType
            guard kind == FileAttributeType.typeRegular else {
                throw helperFailure("write_failed", "Путь сохранения скана занят не обычным файлом.",
                                    exit: ExitCode.transferFailed)
            }
        }
    }

    // MARK: Main run loop

    func run() {
        installSignalSources()
        let browser = ICDeviceBrowser()
        self.browser = browser
        browser.delegate = self
        browser.browsedDeviceTypeMask = ScannerHelper.scannerTypeMask()
        browser.start()

        while true {
            if cancelRequested {
                abortScan()
                reportAndExit(helperFailure("cancelled", "Сканирование отменено.",
                                            exit: ExitCode.cancelled))
            }
            if Date() >= deadline {
                abortScan()
                if options.command == "scan" && stage == .browsing {
                    reportAndExit(helperFailure("device_not_found", "Выбранный сканер не найден.",
                                                exit: ExitCode.deviceNotFound))
                }
                reportAndExit(helperFailure("timeout",
                    "Операция сканирования не завершилась за \(Int(options.timeout)) с.",
                    exit: ExitCode.timeout))
            }
            ingestBrowsedDevices()
            if options.command == "list" {
                if shouldFinishListing() {
                    browser.stop()
                    emitJSON(listResult())
                    exit(ExitCode.ok)
                }
            } else {
                advance()
            }
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
    }

    private static func scannerTypeMask() -> ICDeviceTypeMask {
        var raw = ICDeviceTypeMask.scanner.rawValue
        raw |= ICDeviceLocationTypeMask.local.rawValue
        raw |= ICDeviceLocationTypeMask.shared.rawValue
        raw |= ICDeviceLocationTypeMask.bonjour.rawValue
        raw |= ICDeviceLocationTypeMask.remote.rawValue
        return ICDeviceTypeMask(rawValue: raw) ?? ICDeviceTypeMask.scanner
    }

    private func installSignalSources() {
        for number in [SIGINT, SIGTERM] {
            signal(number, SIG_IGN)
            let source = DispatchSource.makeSignalSource(signal: number, queue: DispatchQueue.main)
            source.setEventHandler { [weak self] in
                self?.cancelRequested = true
            }
            source.resume()
            signalSources.append(source)
        }
    }

    private func ingestBrowsedDevices() {
        guard let browsed = browser?.devices else { return }
        for device in browsed {
            guard let scanner = device as? ICScannerDevice else { continue }
            if !scanners.contains(where: { $0 === scanner }) {
                scanners.append(scanner)
            }
        }
    }

    private func shouldFinishListing() -> Bool {
        let elapsed = Date().timeIntervalSince(startTime)
        if elapsed < Limits.listingMinimumSeconds { return false }
        if Date().timeIntervalSince(recentDeviceChange) < Limits.listingSettleSeconds { return false }
        if enumerationFinished { return true }
        return elapsed >= Limits.listingFallbackSeconds
    }

    private func listResult() -> [String: Any] {
        var devices: [[String: Any]] = []
        for scanner in scanners {
            devices.append(ScannerHelper.describe(scanner))
        }
        return [
            "ok": true,
            "command": "list",
            "helper": Limits.helperVersion,
            "devices": devices,
        ]
    }

    private static func describe(_ device: ICScannerDevice) -> [String: Any] {
        let name = (device.name?.isEmpty == false) ? (device.name ?? "") : "Сканер"
        return [
            "id": deviceIdentifier(device),
            "name": name,
            "manufacturer": "",
            "description": device.locationDescription ?? "",
            "transport": device.transportType ?? "",
        ]
    }

    private static func deviceIdentifier(_ device: ICScannerDevice) -> String {
        if let uuid = device.uuidString, !uuid.isEmpty { return uuid }
        if let persistent = device.persistentIDString, !persistent.isEmpty { return persistent }
        return "name:" + (device.name ?? "Сканер")
    }

    // MARK: Scan state machine

    private func advance() {
        switch stage {
        case .browsing:
            if let device = matchDevice() {
                startSession(on: device)
            }
        case .closingSession:
            guard let device = targetDevice else {
                finishSuccessfully()
                return
            }
            if !device.hasOpenSession {
                finishSuccessfully()
            } else if let closeDeadline = closeDeadline, Date() >= closeDeadline {
                finishSuccessfully()
            }
        default:
            break
        }
    }

    private func matchDevice() -> ICScannerDevice? {
        let wanted = options.deviceID
        for scanner in scanners where ScannerHelper.deviceIdentifier(scanner) == wanted {
            return scanner
        }
        for scanner in scanners where (scanner.name ?? "") == wanted {
            return scanner
        }
        return nil
    }

    private func startSession(on device: ICScannerDevice) {
        targetDevice = device
        device.delegate = self
        device.transferMode = ICScannerTransferMode.fileBased
        device.downloadsDirectory = uploadDirectory
        device.documentName = documentBaseName
        device.documentUTI = Limits.outputUTI
        stage = .openingSession
        device.requestOpenSession()
    }

    private static func preferredUnitTypes(for device: ICScannerDevice) -> [ICScannerFunctionalUnitType] {
        var available = Set<UInt>()
        for number in device.availableFunctionalUnitTypes {
            available.insert(number.uintValue)
        }
        var ordered: [ICScannerFunctionalUnitType] = []
        let candidates: [ICScannerFunctionalUnitType] = [.flatbed, .documentFeeder]
        for candidate in candidates {
            if available.isEmpty || available.contains(candidate.rawValue) {
                ordered.append(candidate)
            }
        }
        return ordered
    }

    private func selectNextUnit(_ scanner: ICScannerDevice, reason: String) {
        if let next = pendingUnitTypes.first {
            pendingUnitTypes.removeFirst()
            scanner.requestSelect(next)
            return
        }
        reportAndExit(helperFailure("no_functional_unit",
            "Не удалось выбрать модуль сканирования: \(reason)", exit: ExitCode.transferFailed))
    }

    private func applyResolution(_ unit: ICScannerFunctionalUnit) {
        let supported = unit.supportedResolutions
        if supported.isEmpty {
            actualDPI = unit.resolution
            return
        }
        if supported.contains(options.dpi) {
            unit.resolution = options.dpi
        } else {
            var best = supported.first ?? unit.resolution
            var bestDelta = abs(best - options.dpi)
            for value in supported {
                let delta = abs(value - options.dpi)
                if delta < bestDelta {
                    best = value
                    bestDelta = delta
                }
            }
            unit.resolution = best
        }
        actualDPI = unit.resolution
    }

    private func completeScan() {
        guard !finalizing, stage == .scanning else { return }
        finalizing = true
        var candidates: [URL] = []
        if let scanned = scannedURL {
            candidates.append(scanned)
        }
        if let located = locateProducedFile() {
            candidates.append(located)
        }
        guard let sourceURL = candidates.first(where: {
            FileManager.default.fileExists(atPath: $0.path)
        }) else {
            reportAndExit(helperFailure("transfer_failed", "Сканер не вернул файл изображения.",
                                        exit: ExitCode.transferFailed))
        }
        do {
            let size = try finalize(source: sourceURL)
            guard size > 0 else {
                reportAndExit(helperFailure("transfer_failed", "Сканер вернул пустой файл изображения.",
                                            exit: ExitCode.transferFailed))
            }
            stage = .closingSession
            closeDeadline = Date().addingTimeInterval(Limits.closeSessionGraceSeconds)
            if let device = targetDevice, device.hasOpenSession {
                device.requestCloseSession()
            } else {
                finishSuccessfully()
            }
        } catch let failure as HelperFailure {
            reportAndExit(failure)
        } catch {
            reportAndExit(helperFailure("write_failed", error.localizedDescription,
                                        exit: ExitCode.transferFailed))
        }
    }

    private func locateProducedFile() -> URL? {
        let fileManager = FileManager.default
        guard let names = try? fileManager.contentsOfDirectory(atPath: uploadDirectory.path) else {
            return nil
        }
        var best: URL?
        var bestDate = startTime
        for name in names {
            guard name.hasPrefix(documentBaseName) else { continue }
            let candidate = uploadDirectory.appendingPathComponent(name, isDirectory: false)
            if candidate.standardizedFileURL == outputURL.standardizedFileURL { continue }
            var isDirectory: ObjCBool = false
            guard fileManager.fileExists(atPath: candidate.path, isDirectory: &isDirectory),
                  !isDirectory.boolValue else { continue }
            guard let attributes = try? fileManager.attributesOfItem(atPath: candidate.path),
                  let modified = attributes[.modificationDate] as? Date,
                  modified >= bestDate else { continue }
            bestDate = modified
            best = candidate
        }
        return best
    }

    /// Normalises whatever the device module produced into the requested output
    /// file. The destination is an app owned temporary path validated in init;
    /// the driver's own file is removed afterwards.
    private func finalize(source: URL) throws -> Int {
        guard let imageSource = CGImageSourceCreateWithURL(source as CFURL, nil),
              CGImageSourceGetCount(imageSource) > 0,
              let image = CGImageSourceCreateImageAtIndex(imageSource, 0, nil) else {
            throw helperFailure("invalid_image", "Сканер не создал корректный файл изображения.",
                                exit: ExitCode.transferFailed)
        }
        let fileManager = FileManager.default
        if fileManager.fileExists(atPath: outputURL.path) {
            let attributes = try? fileManager.attributesOfItem(atPath: outputURL.path)
            guard (attributes?[.type] as? FileAttributeType) == FileAttributeType.typeRegular else {
                throw helperFailure("write_failed", "Путь сохранения скана занят не обычным файлом.",
                                    exit: ExitCode.transferFailed)
            }
            do {
                try fileManager.removeItem(at: outputURL)
            } catch {
                throw helperFailure("write_failed", "Не удалось подготовить файл результата сканирования.",
                                    exit: ExitCode.transferFailed)
            }
        }
        guard let destination = CGImageDestinationCreateWithURL(outputURL as CFURL,
                                                               Limits.outputUTI as CFString, 1, nil) else {
            throw helperFailure("write_failed", "Не удалось создать файл результата сканирования.",
                                exit: ExitCode.transferFailed)
        }
        let properties: [CFString: Any] = [kCGImageDestinationLossyCompressionQuality: 0.92]
        CGImageDestinationAddImage(destination, image, properties as CFDictionary)
        guard CGImageDestinationFinalize(destination) else {
            throw helperFailure("write_failed", "Не удалось записать файл результата сканирования.",
                                exit: ExitCode.transferFailed)
        }
        if source.standardizedFileURL != outputURL.standardizedFileURL {
            try? fileManager.removeItem(at: source)
        }
        let attributes = try? fileManager.attributesOfItem(atPath: outputURL.path)
        return ((attributes?[.size] as? NSNumber)?.intValue) ?? 0
    }

    private func finishSuccessfully() {
        browser?.stop()
        let attributes = try? FileManager.default.attributesOfItem(atPath: outputURL.path)
        let size = ((attributes?[.size] as? NSNumber)?.intValue) ?? 0
        emitJSON([
            "ok": true,
            "command": "scan",
            "helper": Limits.helperVersion,
            "format": "JPEG",
            "uti": Limits.outputUTI,
            "bytes": size,
            "dpi": actualDPI,
            "requested_dpi": options.dpi,
        ])
        exit(ExitCode.ok)
    }

    private func abortScan() {
        browser?.stop()
        guard let device = targetDevice else { return }
        if stage == .scanning {
            device.cancelScan()
        }
        if device.hasOpenSession {
            device.requestCloseSession()
        }
        let grace = Date().addingTimeInterval(1.5)
        while Date() < grace && device.hasOpenSession {
            RunLoop.main.run(until: Date().addingTimeInterval(0.05))
        }
    }

    // MARK: ICDeviceBrowserDelegate

    func deviceBrowser(_ browser: ICDeviceBrowser, didAdd device: ICDevice, moreComing: Bool) {
        recentDeviceChange = Date()
        guard let scanner = device as? ICScannerDevice else { return }
        if !scanners.contains(where: { $0 === scanner }) {
            scanners.append(scanner)
        }
    }

    func deviceBrowser(_ browser: ICDeviceBrowser, didRemove device: ICDevice, moreGoing: Bool) {
        recentDeviceChange = Date()
        scanners.removeAll { $0 === device }
    }

    func deviceBrowserDidEnumerateLocalDevices(_ browser: ICDeviceBrowser) {
        recentDeviceChange = Date()
        enumerationFinished = true
    }

    func deviceBrowser(_ browser: ICDeviceBrowser, requestsSelect device: ICDevice) {
        // PdfDesk always selects the device explicitly from the dialog; an
        // automatic selection request from the system is ignored on purpose.
    }

    // MARK: ICDeviceDelegate

    func device(_ device: ICDevice, didOpenSessionWithError error: Error?) {
        guard stage == .openingSession, let scanner = device as? ICScannerDevice else { return }
        if let error = error {
            reportAndExit(helperFailure("session_failed",
                "Не удалось открыть сеанс сканера: \(error.localizedDescription)",
                exit: ExitCode.transferFailed))
        }
        pendingUnitTypes = ScannerHelper.preferredUnitTypes(for: scanner)
        if pendingUnitTypes.isEmpty {
            reportAndExit(helperFailure("no_functional_unit",
                "Сканер не предоставил доступный модуль сканирования.", exit: ExitCode.transferFailed))
        }
        let next = pendingUnitTypes.removeFirst()
        stage = .selectingUnit
        scanner.requestSelect(next)
    }

    func device(_ device: ICDevice, didCloseSessionWithError error: Error?) {
        guard stage == .closingSession else { return }
        finishSuccessfully()
    }

    func deviceDidBecomeReady(_ device: ICDevice) {
        // The scan request is issued from the functional unit selection callback.
    }

    func device(_ device: ICDevice, didEncounterError error: Error?) {
        // Errors reported by unrelated browsed devices must not abort the
        // operation; only the device that owns the selected session is fatal.
        guard stage != .browsing, stage != .closingSession else { return }
        guard let scanner = device as? ICScannerDevice, let target = targetDevice,
              scanner === target, let error = error else { return }
        reportAndExit(helperFailure("device_error",
            "Сканер сообщил об ошибке: \(error.localizedDescription)", exit: ExitCode.transferFailed))
    }

    // MARK: ICScannerDeviceDelegate

    func scannerDevice(_ scanner: ICScannerDevice,
                       didSelect functionalUnit: ICScannerFunctionalUnit, error: Error?) {
        guard stage == .selectingUnit else { return }
        if let error = error {
            selectNextUnit(scanner, reason: error.localizedDescription)
            return
        }
        if let feeder = functionalUnit as? ICScannerFunctionalUnitDocumentFeeder, !feeder.documentLoaded {
            selectNextUnit(scanner, reason: "в податчике документов нет листа")
            return
        }
        applyResolution(functionalUnit)
        stage = .scanning
        scanner.requestScan()
    }

    func scannerDevice(_ scanner: ICScannerDevice, didScanTo url: URL) {
        scannedURL = url
        if scanCompleted {
            completeScan()
        }
    }

    func scannerDevice(_ scanner: ICScannerDevice, didCompleteScanWithError error: Error?) {
        guard stage == .scanning else { return }
        if let error = error {
            reportAndExit(helperFailure("transfer_failed",
                "Сканирование завершилось ошибкой: \(error.localizedDescription)",
                exit: ExitCode.transferFailed))
        }
        scanCompleted = true
        completeScan()
    }
}

// MARK: - Entry point

func runHelper() -> Never {
    let arguments = Array(CommandLine.arguments.dropFirst())
    let options: HelperOptions
    do {
        options = try parseArguments(arguments)
    } catch let failure as HelperFailure {
        reportAndExit(failure)
    } catch {
        reportAndExit(helperFailure("internal", "Непредвиденная ошибка разбора параметров.",
                                    exit: ExitCode.usage))
    }
    do {
        let helper = try ScannerHelper.make(options: options)
        helper.run()
    } catch let failure as HelperFailure {
        reportAndExit(failure)
    } catch {
        reportAndExit(helperFailure("internal", error.localizedDescription,
                                    exit: ExitCode.transferFailed))
    }
    reportAndExit(helperFailure("internal", "Помощник завершился без результата.",
                                exit: ExitCode.transferFailed))
}

@main
struct MacScannerMain {
    static func main() {
        runHelper()
    }
}
