import re
import resource
import subprocess
import time
from multiprocessing import Process
from pathlib import Path

import demistomock as demisto  # noqa: F401
import requests
import urllib3
from CommonServerPython import *  # noqa: F401

urllib3.disable_warnings()

CLOUD_METADATA_URL = "http://169.254.169.254/"  # disable-secrets-detection


def big_string(size):
    s = "a" * 1024
    while len(s) < size:
        s = s * 2
    return len(s)


def mem_size_to_bytes(mem: str) -> int:
    res = re.match(r"(\d+)\s*([gm])?b?", mem, re.IGNORECASE)
    if not res:
        raise ValueError(f"Failed parsing memory string: {mem}")
    b = int(res.group(1))
    if res.group(2):
        b = b * 1024 * 1024  # convert to mega byte
        if res.group(2).lower() == "g":
            b = b * 1024  # convert to giga
    return b


def check_memory(target_mem: str, check_type: str) -> str:  # pragma: no cover
    """Check allocating memory

    Arguments:
        target_mem {str} -- target memory size. Can specify as 1g 1m and so on
        check_type {str} -- How to check either: cgroup (check configuration of cgroup) or allocate (check actual allocation)

    Returns:
        str -- error string if failed
    """
    size = mem_size_to_bytes(target_mem)
    if check_type == "allocate":
        LOG(f"starting process to check memory of size: {size}")
        p = Process(target=big_string, args=(size,))
        p.start()
        p.join()
        LOG(f"memory intensive process status code: {p.exitcode}")
        if p.exitcode == 0:
            return (
                f"Succeeded allocating memory of size: {target_mem}. "
                "It seems that you haven't limited the available memory to the docker container."
            )
    else:
        cgroup_file_v1 = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
        cgroup_file_v2 = "/sys/fs/cgroup/memory.max"
        if Path(cgroup_file_v1).exists():
            cgroup_file = cgroup_file_v1
        elif Path(cgroup_file_v2).exists():
            cgroup_file = cgroup_file_v2
        else:
            return (
                "Failed checking cgroup file, memory_check set to cgroup but neither v1 or v2"
                " cgroup files found, verify cgroups is enabled."
            )

        try:
            with open(cgroup_file) as f:
                mem_bytes = int(f.read().strip())
                if mem_bytes > size:
                    return (
                        f"According to memory cgroup configuration at: {cgroup_file}"
                        f" available memory in bytes [{mem_bytes}] is larger than {target_mem}"
                    )
        except Exception as ex:
            return (
                f"Failed reading memory cgroup from: {cgroup_file}. Err: {ex}."
                " You may be running a docker version which does not provide this configuration information."
                " You can try running the memory check with memory_check=allocate as an alternative."
            )
    return ""


def check_pids(pid_num: int) -> str:
    LOG(f"Starting pid check for: {pid_num}")
    processes = [Process(target=time.sleep, args=(30,)) for i in range(pid_num)]
    try:
        for p in processes:
            p.start()
        time.sleep(0.5)
        alive = 0
        for p in processes:
            if p.is_alive():
                alive += 1
        if alive >= pid_num:
            return (
                f"Succeeded creating processs of size: {pid_num}. "
                "It seems that you haven't limited the available pids to the docker container."
            )
        else:
            LOG(f"Number of processes that are alive: {alive} is smaller than {pid_num}. All good.")
    except Exception as ex:
        LOG(f"Pool startup failed (as expected): {ex}")
    finally:
        for p in processes:
            if p.is_alive():
                p.terminate()
                p.join()
    return ""


def check_fd_limits(soft, hard) -> str:
    s, h = resource.getrlimit(resource.RLIMIT_NOFILE)
    if s > soft:
        return f"FD soft limit: {s} is above desired limt: {soft}."
    if h > hard:
        return f"FD hard limit: {h} is above desired limit: {hard}."
    return ""


def check_non_root():
    uid = os.getuid()
    if uid == 0:
        return (
            f"Running as root with uid: {uid}."
            " It seems that you haven't set the docker container to run with a non-root internal user."
        )
    return ""


def intensive_calc(iter: int):
    i = 0
    x = 1
    while i < iter:
        x = x * 2
        i += 1
    return x


def check_cpus(num_cpus: int) -> str:
    iterval = 500 * 1000
    processes = [Process(target=intensive_calc, args=(iterval,)) for i in range(num_cpus)]
    start = time.time_ns()
    for p in processes:
        p.start()
    for p in processes:
        p.join()
    runtime = time.time_ns() - start
    LOG(f"cpus check runtime for {num_cpus} processes time: {runtime}")
    processes = [Process(target=intensive_calc, args=(iterval,)) for i in range(num_cpus * 2)]
    start = time.time_ns()
    for p in processes:
        p.start()
    for p in processes:
        p.join()
    runtime2 = time.time_ns() - start
    # runtime 2 should be 2 times slower. But we give it a safty as the machine itself maybe loaded
    LOG(f"cpus check runtime for {num_cpus * 2} processes time: {runtime2}")
    if runtime2 < runtime * 1.5:
        return (
            "CPU processing power increased significantly when increasing processes "
            f"from: {num_cpus} (time: {runtime}) to: {num_cpus * 2} (time: {runtime2}). "
            "Note: this test may fail even if the proper configuration has been applied and"
            " the machine itself is loaded."
        )
    return ""


def get_default_gateway():
    res = subprocess.check_output(["ip", "route", "list"], text=True, stderr=subprocess.STDOUT)
    LOG(f"result of ip route list: {res}")
    line1 = res.splitlines()[0]
    if not line1.startswith("default via"):
        raise ValueError(f'Excpected "ip route list" to start with "default via" but not found. Got: [{line1}]')
    return line1.split()[2]


def check_network(network_check: str) -> str:
    """
    Check that Cloud provider metadata service is not exposed and that access to localhost is not available.
    """
    return_res = ""
    if network_check in ("all", "cloud_metadata"):
        LOG("Check cloud metadata server access...")
        try:
            res = requests.get(CLOUD_METADATA_URL, timeout=1)
            LOG(f"cloud metadata server returned successfuly: {res.status_code} {res.headers}")
            return_res += (
                f"Access to cloud metadata server: {CLOUD_METADATA_URL} is open. It seems that you haven't blocked "
                f"access to the cloud metadata server. Response status code: [{res.status_code}]. "
                f"Response headers: {res.headers}"
            )
        except Exception as ex:
            LOG(f"Cloud metadata server returned an exception (this is good. It means there is no access to the server.): {ex}")
    if network_check in ("all", "host_machine"):
        LOG("Check host access")
        gateway_ip = get_default_gateway()
        try:
            res = requests.get(f"https://{gateway_ip}/", verify=False, timeout=1)  # nosec # guardrails-disable-line
            LOG(f"Host https request returned successfully: {res.status_code} {res.headers}")
            if return_res:
                return_res += "\n"
            return_res += (
                f"Access to host server via default gateway ip: {gateway_ip} is open. It seems that "
                f"you haven't blocked access to the host server. Response status code: [{res.status_code}]."
                f"Response headers: {res.headers}"
            )
        except Exception as ex:
            LOG(
                "The host gateway server returned an exception (this is good."
                f" It means that there is no access to the host server.): {ex}"
            )
    return return_res


def main():
    if os.getenv("container") == "podman":
        return_error("This script only works in Docker containers. Podman is not supported")
        return

    mem = demisto.args().get("memory", "1g")
    mem_check = demisto.args().get("memory_check", "cgroup")
    network_check = demisto.args().get("network_check", "all")
    pids = int(demisto.args().get("pids", 256))
    fds_soft = int(demisto.args().get("fds_soft", 1024))
    fds_hard = int(demisto.args().get("fds_hard", 8192))
    cpus = int(demisto.args().get("cpus", 1))
    success = "Success"
    check = "Check"
    status = "Status"
    res = [
        {
            check: "Non-root User",
            status: check_non_root() or success,
        },
        {
            check: "Memory",
            status: check_memory(mem, mem_check) or success,
        },
        {
            check: "File Descriptors",
            status: check_fd_limits(fds_soft, fds_hard) or success,
        },
        {
            check: "CPUs",
            status: check_cpus(cpus) or success,
        },
        {
            check: "PIDs",
            status: check_pids(pids) or success,
        },
        {check: "Network", status: check_network(network_check) or success},
    ]
    failed = False
    failed_msg = ""
    for v in res:
        if v[status] != success:
            failed = True
            v[status] = "Failed: " + v[status]
            failed_msg += f"* {v[status]}\n"
    table = tableToMarkdown("Docker Hardening Results Check", res, [check, status])
    return_outputs(table)
    if failed:
        return_error(
            f"Failed verifying docker hardening:\n{failed_msg}"
            "More details at: https://docs-cortex.paloaltonetworks.com/r/Cortex-XSOAR/6.10/Cortex-XSOAR-Administrator-Guide/Docker-Hardening-Guide"
        )  # noqa


# python2 uses __builtin__ python3 uses builtins
if __name__ == "__builtin__" or __name__ == "builtins":
    main()
