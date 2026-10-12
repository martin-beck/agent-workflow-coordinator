#!/usr/bin/env bash
# Copyright (C) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# SPDX-License-Identifier: MIT
set -euo pipefail

if [[ "${1:-}" != "--tier" || ( "${2:-}" != "portable-smoke" && "${2:-}" != "pr-fast" && "${2:-}" != "pr-publication" && "${2:-}" != "full-exhaustive" ) ]]; then
    echo "usage: $0 --tier portable-smoke|pr-fast|pr-publication|full-exhaustive [--diagnostic-queue ABSOLUTE_PATH --diagnostic-admission-lock ABSOLUTE_PATH]" >&2
    exit 64
fi
readonly TIER="$2"
shift 2
EXECUTION_CLASSIFICATION=canonical
RUNNER_ADMISSION_ARGS=()
if [[ "$#" -eq 4 && "$1" == "--diagnostic-queue" && "$3" == "--diagnostic-admission-lock" ]]; then
    if [[ "$2" != /* || "$4" != /* || "$2" == "$4" ]]; then
        echo "diagnostic admission paths must be distinct absolute paths" >&2
        exit 64
    fi
    EXECUTION_CLASSIFICATION=diagnostic-private-admission
    RUNNER_ADMISSION_ARGS=(--queue "$2" --admission-lock "$4")
elif [[ "$#" -ne 0 ]]; then
    echo "diagnostic queue and admission lock must be supplied together after --tier" >&2
    exit 64
fi
readonly EXECUTION_CLASSIFICATION
readonly ATTESTATION="${TLC_ATTESTATION_PATH:-${TMPDIR:-/tmp}/handoffctl-${TIER}-attestation.json}"

# The runner and receipt must describe the same effective resource contract.
# Workflows normally provide these values, but a direct local invocation must
# remain self-contained rather than completing TLC and failing at attestation.
export TLC_MEMORY_MAX="${TLC_MEMORY_MAX:-3G}"
export TLC_SWAP_MAX="${TLC_SWAP_MAX:-3G}"

readonly TLA_VERSION=1.7.4
readonly TLA_SHA256=936a262061c914694dfd669a543be24573c45d5aa0ff20a8b96b23d01e050e88
readonly SPEC_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly TEMP_DIR="$(mktemp -d)"
readonly SLICE_DIR="${SPEC_DIR}/.tlc-slices-$$"
readonly MANIFEST="${TEMP_DIR}/outcomes.manifest"
: > "${MANIFEST}"
trap 'rm -rf -- "${TEMP_DIR}" "${SLICE_DIR}"' EXIT

readonly JAR="${TEMP_DIR}/tla2tools.jar"
readonly URL="https://github.com/tlaplus/tlaplus/releases/download/v${TLA_VERSION}/tla2tools.jar"

curl --fail --location --retry 3 --show-error --silent --output "${JAR}" "${URL}"
printf '%s  %s\n' "${TLA_SHA256}" "${JAR}" | sha256sum --check --strict

run_model() {
    local model="$1"
    local source="${2:-${model}}"
    local config="${3:-${SPEC_DIR}/${model}.cfg}"
    local model_path
    # The fast tier has its own reduced configuration over the binding spec.
    if [[ "${model}" == "HandoffctlFast" ]]; then
        source=HandoffctlBinding
    elif [[ "${model}" == "HandoffctlReceiptsFast" ]]; then
        source=HandoffctlReceipts
    elif [[ "${model}" == "HandoffctlPublicationFast" ]]; then
        source=HandoffctlPublication
        config="${SPEC_DIR}/HandoffctlPublicationFast.cfg"
    elif [[ "${model}" == "OracleInteractionGates" ]]; then
        source=../oracle/OracleInteractionGates
        config="${SPEC_DIR}/../oracle/OracleInteractionGates.cfg"
    fi
    if [[ "${source}" == /* ]]; then
        model_path="${source}.tla"
    else
        model_path="${SPEC_DIR}/${source}.tla"
    fi
    python3 "${SPEC_DIR}/../../tools/tlc_runner.py" \
        --jar "${JAR}" \
        --model "${model_path}" \
        --config "${config}" \
        --metadir "${TEMP_DIR}/${model}-states" \
        "${RUNNER_ADMISSION_ARGS[@]}"
    printf "%s success\n" "${model}" >> "${MANIFEST}"
}

if [[ "${TIER}" == "portable-smoke" ]]; then
    # Smoke is deliberately non-exhaustive and never produces full evidence.
    run_model HandoffctlBinding
elif [[ "${TIER}" == "pr-fast" ]]; then
    # Deliberately smaller safety-only required merge gate.
    run_model HandoffctlFast
    run_model HandoffctlReceiptsFast HandoffctlReceipts
    run_model HandoffctlPublicationFast HandoffctlPublication
    run_model HandoffctlObservation
    run_model OracleInteractionGates
elif [[ "${TIER}" == "pr-publication" ]]; then
    # PR publication checks every invariant family. The general lifecycle
    # model uses a one-process configuration; weekly full evidence retains its
    # complete two-process cross-product.
    run_model HandoffctlBinding
    run_model HandoffctlLocks
    run_model HandoffctlRun
    run_model HandoffctlStorage
    run_model HandoffctlReceipts
    run_model HandoffctlPublication
    run_model HandoffctlObservation
    run_model HandoffctlPR Handoffctl
    run_model HandoffctlRecovery
else
    run_model HandoffctlBinding
    run_model HandoffctlLocks
    run_model HandoffctlRun
    run_model HandoffctlStorage
    run_model HandoffctlReceipts
    run_model HandoffctlPublication
    run_model HandoffctlObservation
    # `operation` is immutable. These p1 operation slices partition the
    # original two-process aggregate model exactly; p2 retains its full domain.
    mapfile -t local_operations < <(
        cd "${SPEC_DIR}" && python3 -c 'from slices import OPERATIONS; print(*OPERATIONS, sep="\n")'
    )
    mkdir --mode=700 "${SLICE_DIR}"
    for operation_name in "${local_operations[@]}"; do
        slice_module="$(cd "${SPEC_DIR}" && python3 -c 'import sys; from slices import module_name; print(module_name(sys.argv[1]))' "${operation_name}")"
        slice="${SLICE_DIR}/${slice_module}"
        python3 "${SPEC_DIR}/slices.py" --base-source "${SPEC_DIR}/Handoffctl.tla" --operation "${operation_name}" --output-source "${slice}.tla"
        cp -- "${SPEC_DIR}/Handoffctl.cfg" "${slice}.cfg"
        run_model "HandoffctlSlice-${operation_name}" "${slice}" "${slice}.cfg"
    done
    run_model HandoffctlRecovery
fi
python3 "${SPEC_DIR}/attest.py" --tier "${TIER}" --output "${ATTESTATION}" --jar "${JAR}" --manifest "${MANIFEST}" --execution-classification "${EXECUTION_CLASSIFICATION}" \
    --models $(if [[ "${TIER}" == "portable-smoke" ]]; then echo HandoffctlBinding; elif [[ "${TIER}" == "pr-fast" ]]; then echo HandoffctlFast HandoffctlReceiptsFast HandoffctlPublicationFast HandoffctlObservation OracleInteractionGates; elif [[ "${TIER}" == "pr-publication" ]]; then echo HandoffctlBinding HandoffctlLocks HandoffctlRun HandoffctlStorage HandoffctlReceipts HandoffctlPublication HandoffctlObservation HandoffctlPR HandoffctlRecovery; else printf 'HandoffctlBinding HandoffctlLocks HandoffctlRun HandoffctlStorage HandoffctlReceipts HandoffctlPublication HandoffctlObservation '; (cd "${SPEC_DIR}" && python3 -c 'from slices import OPERATIONS; print(*(f"HandoffctlSlice-{operation}" for operation in OPERATIONS))'); echo HandoffctlRecovery; fi)
