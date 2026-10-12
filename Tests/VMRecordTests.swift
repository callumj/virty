import Foundation

@main
struct VMRecordTests {
    static func main() throws {
        let legacy = """
        {"id":"00000000-0000-0000-0000-000000000001","username":"virty","keychain":"broker",
         "installed":true,"provisioned":false,"restoreBuild":"26A434","cpuCount":4,
         "memoryBytes":6442450944,"width":1280,"height":800,"macAddress":"02:00:00:00:00:01",
         "managerExecutable":"/public-test/virty-manager"}
        """
        var record = try JSONDecoder().decode(VMRecord.self, from: Data(legacy.utf8))
        precondition(record.needsGuestProvisioning && record.accountSetup == "automatic")
        record.provisioned = true
        precondition(!record.needsGuestProvisioning)
        record.provisioned = false
        record.setupAssistant = true
        precondition(!record.needsGuestProvisioning && record.accountSetup == "setup-assistant")
        record = try JSONDecoder().decode(VMRecord.self, from: JSONEncoder().encode(record))
        precondition(record.setupAssistant == true && !record.provisioned && !record.needsGuestProvisioning)
        record.setupAssistant = false
        precondition(record.needsGuestProvisioning)
        record.guestOS = "linux"
        precondition(!record.needsGuestProvisioning && record.accountSetup == "guest-managed")
        print("VM record checks passed: legacy provisioning, persistent Setup Assistant policy and Linux isolation.")
    }
}
