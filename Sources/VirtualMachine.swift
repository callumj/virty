import AppKit
import Virtualization

@MainActor
func refreshCloneIdentity(home: URL) throws {
    var record = try VMRecord.load(home)
    let identity = record.os == "macos" ? VZMacMachineIdentifier().dataRepresentation : VZGenericMachineIdentifier().dataRepresentation
    try identity.write(to: home.appendingPathComponent("machine-id.bin"), options: .atomic)
    record.macAddress = VZMACAddress.randomLocallyAdministered().string
    try record.save(home)
    _ = try configuration(home: home, record: record)
    emit(["ok": true, "vmID": record.id, "identityRegenerated": true])
}

@MainActor
func configuration(home: URL, record: VMRecord) throws -> VZVirtualMachineConfiguration {
    let config = VZVirtualMachineConfiguration()
    if record.os == "linux" {
        let platform = VZGenericPlatformConfiguration()
        guard let identifier = VZGenericMachineIdentifier(dataRepresentation: try Data(contentsOf: home.appendingPathComponent("machine-id.bin"))) else {
            throw Failure("Stored Linux machine identity is invalid.")
        }
        platform.machineIdentifier = identifier
        config.platform = platform
        let loader = VZEFIBootLoader()
        loader.variableStore = VZEFIVariableStore(url: home.appendingPathComponent("efi.bin"))
        config.bootLoader = loader
        let graphics = VZVirtioGraphicsDeviceConfiguration()
        graphics.scanouts = [VZVirtioGraphicsScanoutConfiguration(widthInPixels: record.width, heightInPixels: record.height)]
        config.graphicsDevices = [graphics]
    } else if record.os == "macos" {
        let platform = VZMacPlatformConfiguration()
        guard let model = VZMacHardwareModel(dataRepresentation: try Data(contentsOf: home.appendingPathComponent("hardware-model.bin"))),
              model.isSupported,
              let identifier = VZMacMachineIdentifier(dataRepresentation: try Data(contentsOf: home.appendingPathComponent("machine-id.bin"))) else {
            throw Failure("Stored macOS hardware model or machine identity is unsupported.")
        }
        platform.hardwareModel = model
        platform.machineIdentifier = identifier
        platform.auxiliaryStorage = VZMacAuxiliaryStorage(url: home.appendingPathComponent("auxiliary-storage.bin"))
        config.platform = platform
        config.bootLoader = VZMacOSBootLoader()
        let graphics = VZMacGraphicsDeviceConfiguration()
        graphics.displays = [VZMacGraphicsDisplayConfiguration(widthInPixels: record.width, heightInPixels: record.height, pixelsPerInch: 80)]
        config.graphicsDevices = [graphics]
    } else { throw Failure("Unsupported guest OS.") }
    config.cpuCount = record.cpuCount
    config.memorySize = record.memoryBytes
    let disk = try VZDiskImageStorageDeviceAttachment(url: home.appendingPathComponent("disk.img"), readOnly: false)
    config.storageDevices = [VZVirtioBlockDeviceConfiguration(attachment: disk)]
    if let media = record.installMedia {
        guard record.os == "linux", media.hasPrefix("/") else { throw Failure("Invalid Linux installation media.") }
        let attachment = try VZDiskImageStorageDeviceAttachment(url: URL(fileURLWithPath: media), readOnly: true)
        let cd = VZUSBMassStorageDeviceConfiguration(attachment: attachment)
        config.storageDevices.append(cd)
        config.usbControllers = [VZXHCIControllerConfiguration()]
    }
    config.keyboards = [VZUSBKeyboardConfiguration()]
    config.pointingDevices = [VZUSBScreenCoordinatePointingDeviceConfiguration()]
    let network = VZVirtioNetworkDeviceConfiguration()
    network.attachment = VZNATNetworkDeviceAttachment()
    guard let address = VZMACAddress(string: record.macAddress) else { throw Failure("Invalid guest MAC address.") }
    network.macAddress = address
    config.networkDevices = [network]
    config.entropyDevices = [VZVirtioEntropyDeviceConfiguration()]
    try config.validate()
    return config
}

@MainActor
func createLinuxGuest(home: URL, media: String, sourceDisk: String, cpus: Int, memoryGiB: Int, diskGiB: Int) throws {
    guard !FileManager.default.fileExists(atPath: home.appendingPathComponent("vm.json").path),
          !FileManager.default.fileExists(atPath: home.appendingPathComponent("disk.img").path) else {
        throw Failure("VM already exists; refusing to overwrite it.")
    }
    guard (1...32).contains(cpus), (1...128).contains(memoryGiB), (4...1024).contains(diskGiB),
          media != "-" || sourceDisk != "-" else { throw Failure("Invalid Linux resources or missing installation media/raw disk.") }
    let record = VMRecord(id: UUID().uuidString, username: "virty", keychain: "broker",
                          installed: true, provisioned: false, restoreBuild: "user-supplied-linux",
                          cpuCount: cpus, memoryBytes: UInt64(memoryGiB) * 1024 * 1024 * 1024,
                          width: 1280, height: 800, macAddress: VZMACAddress.randomLocallyAdministered().string,
                          managerExecutable: Bundle.main.executableURL!.resolvingSymlinksInPath().path,
                          guestOS: "linux", installMedia: media == "-" ? nil : media)
    try VZGenericMachineIdentifier().dataRepresentation.write(to: home.appendingPathComponent("machine-id.bin"))
    _ = try VZEFIVariableStore(creatingVariableStoreAt: home.appendingPathComponent("efi.bin"))
    let disk = home.appendingPathComponent("disk.img")
    if sourceDisk != "-" {
        try FileManager.default.copyItem(at: URL(fileURLWithPath: sourceDisk), to: disk)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: disk.path)
    } else {
        guard FileManager.default.createFile(atPath: disk.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
            throw Failure("Cannot create Linux VM disk.")
        }
        let handle = try FileHandle(forWritingTo: disk)
        try handle.truncate(atOffset: UInt64(diskGiB) * 1024 * 1024 * 1024)
        try handle.close()
    }
    _ = try configuration(home: home, record: record)
    // Blank-disk installs can type this alias in their installer password fields.
    // An imported disk keeps its own accounts; enroll their aliases locally.
    if sourceDisk == "-" { try SecretStore(vmID: record.id, mode: "broker").generate() }
    try record.save(home)
    emit(["created": true, "vmID": record.id, "guestOS": "linux", "installerAttached": record.installMedia != nil,
          "accountSetup": "guest-managed", "keychain": "broker"])
}

@MainActor
func createGuest(home: URL, imageURL: URL, keychain: String, setupAssistant: Bool = false) async throws {
    guard !FileManager.default.fileExists(atPath: home.appendingPathComponent("vm.json").path),
          !FileManager.default.fileExists(atPath: home.appendingPathComponent("disk.img").path) else {
        throw Failure("VM already exists; refusing to overwrite it.")
    }
    guard ["broker", "login", "data-protection"].contains(keychain) else { throw Failure("Unknown Keychain mode.") }
    let image = try await VZMacOSRestoreImage.image(from: imageURL)
    guard let requirements = image.mostFeaturefulSupportedConfiguration else {
        throw Failure("This macOS restore image is not supported on this host.")
    }
    guard setupAssistant || image.operatingSystemVersion.majorVersion >= 27 else {
        throw Failure("Automatic provisioning requires macOS 27+. Use --setup-assistant for an older supported restore image.")
    }
    let record = VMRecord(id: UUID().uuidString, username: setupAssistant ? "" : "virty", keychain: keychain,
                          installed: false, provisioned: false, restoreBuild: image.buildVersion,
                          cpuCount: max(4, requirements.minimumSupportedCPUCount),
                          memoryBytes: max(6 * 1024 * 1024 * 1024, requirements.minimumSupportedMemorySize),
                          width: 1280, height: 800, macAddress: VZMACAddress.randomLocallyAdministered().string,
                          managerExecutable: Bundle.main.executableURL!.resolvingSymlinksInPath().path,
                          setupAssistant: setupAssistant)
    // Generate and store the password before creating a disk. Never send it to stdout.
    try SecretStore(vmID: record.id, mode: record.keychain).generate()
    try requirements.hardwareModel.dataRepresentation.write(to: home.appendingPathComponent("hardware-model.bin"))
    try VZMacMachineIdentifier().dataRepresentation.write(to: home.appendingPathComponent("machine-id.bin"))
    _ = try VZMacAuxiliaryStorage(creatingStorageAt: home.appendingPathComponent("auxiliary-storage.bin"), hardwareModel: requirements.hardwareModel)
    let disk = home.appendingPathComponent("disk.img")
    guard FileManager.default.createFile(atPath: disk.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
        throw Failure("Cannot create VM disk.")
    }
    let handle = try FileHandle(forWritingTo: disk)
    try handle.truncate(atOffset: 64 * 1024 * 1024 * 1024)
    try handle.close()
    try record.save(home)
    _ = try configuration(home: home, record: record)
    emit(["created": true, "vmID": record.id, "username": record.username, "secretAlias": "vm-login", "keychain": record.keychain,
          "accountSetup": record.accountSetup])
}

@MainActor
func installGuest(home: URL, imageURL: URL) async throws {
    var record = try VMRecord.load(home)
    guard record.os == "macos" else { throw Failure("macOS restore cannot be used on a Linux VM.") }
    guard !record.installed else { throw Failure("Guest is already installed; refusing to restore it again.") }
    let image = try await VZMacOSRestoreImage.image(from: imageURL)
    guard image.buildVersion == record.restoreBuild else { throw Failure("Restore image differs from the VM's configured build.") }
    let vm = VZVirtualMachine(configuration: try configuration(home: home, record: record))
    let installer = VZMacOSInstaller(virtualMachine: vm, restoringFromImageAt: imageURL)
    let observation = installer.progress.observe(\.fractionCompleted, options: [.new]) { progress, _ in
        note("Restore progress: \(Int(progress.fractionCompleted * 100))%")
    }
    try await installer.install()
    observation.invalidate()
    record.installed = true
    try record.save(home)
    emit(["installed": true, "vmID": record.id])
}
