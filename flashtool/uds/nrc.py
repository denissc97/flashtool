"""UDS service ids and negative response codes (ISO 14229-1)."""

SERVICES = {
    0x10: "DiagnosticSessionControl",
    0x11: "ECUReset",
    0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation",
    0x22: "ReadDataByIdentifier",
    0x23: "ReadMemoryByAddress",
    0x27: "SecurityAccess",
    0x28: "CommunicationControl",
    0x29: "Authentication",
    0x2E: "WriteDataByIdentifier",
    0x31: "RoutineControl",
    0x34: "RequestDownload",
    0x35: "RequestUpload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
    0x3E: "TesterPresent",
    0x85: "ControlDTCSetting",
}

# Services whose first parameter byte is a sub-function (bit 7 = suppressPosRspMsgIndicationBit).
SUBFUNCTION_SERVICES = {0x10, 0x11, 0x19, 0x27, 0x28, 0x29, 0x31, 0x3E, 0x85}

NRC = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x14: "responseTooLong",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x25: "noResponseFromSubnetComponent",
    0x26: "failurePreventsExecutionOfRequestedAction",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x34: "authenticationRequired",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x50: "certificateVerificationFailed_InvalidTimePeriod",
    0x51: "certificateVerificationFailed_InvalidSignature",
    0x52: "certificateVerificationFailed_InvalidChainOfTrust",
    0x53: "certificateVerificationFailed_InvalidType",
    0x54: "certificateVerificationFailed_InvalidFormat",
    0x55: "certificateVerificationFailed_InvalidContent",
    0x56: "certificateVerificationFailed_InvalidScope",
    0x57: "certificateVerificationFailed_InvalidCertificate",
    0x58: "ownershipVerificationFailed",
    0x59: "challengeCalculationFailed",
    0x5A: "settingAccessRightsFailed",
    0x5B: "sessionKeyCreationDerivationFailed",
    0x5C: "configurationDataUsageFailed",
    0x5D: "deAuthenticationFailed",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x73: "wrongBlockSequenceCounter",
    0x78: "requestCorrectlyReceived-ResponsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
    0x93: "voltageTooLow",
    0x92: "voltageTooHigh",
}


def service_name(sid: int) -> str:
    return SERVICES.get(sid, f"Service 0x{sid:02X}")


def nrc_name(code: int) -> str:
    return NRC.get(code, f"NRC 0x{code:02X}")
