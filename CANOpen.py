import time
import requests
from datetime import datetime

BASE_URL = "http://localhost:9000"
HEADERS = {"accept": "application/json"}

def log(level, message):
    """Prints a clean, timestamped log to the terminal."""
    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")


def format_serial(serial):
    """Formats a serial number to hex string format (e.g., 0x16782149)."""
    if serial is None:
        return "N/A"
    if isinstance(serial, int):
        return f"0x{serial:X}"
    if isinstance(serial, str):
        if serial.startswith("0x") or serial.startswith("0X"):
            return serial
        try:
            return f"0x{int(serial):X}"
        except ValueError:
            return serial
    return str(serial)


def print_exact_hardware_states(snapshot):
    """Prints the hardware states detected on the CAN bus."""
    if not snapshot:
        log("SYSTEM", "No hardware snapshot data available.")
        return

    boot_nodes = snapshot.get("bootNodes", [])
    canopen_nodes = snapshot.get("canOpenNodes", [])

    if not boot_nodes and not canopen_nodes:
        log("SYSTEM", "No devices detected on the CAN bus.")
        return

    print("\n" + "="*50)
    print("           PRESENT DEVICES & STATUS            ")
    print("="*50)
    
    if boot_nodes:
        print("Bootloader Devices:")
        for node in boot_nodes:
            serial = format_serial(node.get("serialNumber"))
            dev_type = node.get("type", "Unknown device")
            print(f" - Bootloader {serial} ({dev_type})")
            
    if canopen_nodes:
        print("Configured CANopen Nodes:")
        for node in canopen_nodes:
            node_id = node.get("nodeID")
            state = node.get("state", "UNKNOWN")
            serial = format_serial(node.get("serialNumber"))
            dev_type = node.get("type", "Unknown device")
            print(f" - CANopen Node: {node_id}: {state}, s/n = {serial}, positionID = 0x0, {dev_type}")
    print("="*50 + "\n")


def Jump_to_preop():
    """Queries /what to get network topology."""
    url = f"{BASE_URL}/what"
    try: 
        response = requests.get(url, headers=HEADERS)
        if response.status_code == 200:
            return response.json()
        log("ERROR", f"Failed to load the network state, gateway returned HTTP {response.status_code}")
        return None
    except requests.exceptions.RequestException as e:
        log("CRITICAL", f"Could not connect to GSSHell backend at localhost:9000. Is it running? {e}")
        return None


def flash_device(serial, node_id, device_type):
    """Flashes the captured serial number with its dynamic Node ID."""
    # We will try both decimal and hex formats to be extremely robust.
    
    # 1. Determine the decimal format
    decimal_serial = None
    if isinstance(serial, int):
        decimal_serial = str(serial)
    elif isinstance(serial, str):
        if serial.startswith("0x") or serial.startswith("0X"):
            try:
                decimal_serial = str(int(serial, 16))
            except ValueError:
                pass
        else:
            try:
                decimal_serial = str(int(serial))
            except ValueError:
                pass
                
    # 2. Determine the hex format
    hex_serial = format_serial(serial)
    
    # Let's try decimal first, then hex
    formats_to_try = []
    if decimal_serial:
        formats_to_try.append((decimal_serial, "decimal"))
    formats_to_try.append((hex_serial, "hex"))
    
    for serial_val, format_type in formats_to_try:
        url = f"{BASE_URL}/flash/{serial_val}/{node_id}"
        try:
            log("ACTION", f"Flashing Serial ({format_type} format): {serial_val} ({device_type}) -> Assigning Node ID: {node_id}")
            # Add delay to let the gateway and hardware stabilize before flash
            time.sleep(1.5)
            response = requests.post(url, headers=HEADERS)
            if response.status_code in [200, 201]:
                log("SUCCESS", f"Node {node_id} identity stored successfully using {format_type} serial {serial_val}.")
                # Add delay to let the device initialize after flashing
                time.sleep(1.5)
                return True
            else:
                log("WARNING", f"Flash attempt failed with {format_type} serial {serial_val}. HTTP Status: {response.status_code}")
        except Exception as e:
            log("ERROR", f"Exception during flash with {format_type} serial {serial_val}: {e}")
            
    log("FAILED", f"All flash attempts failed for Serial {hex_serial} (tried formats: {[f[1] for f in formats_to_try]}).")
    return False


def start_device(node_id):
    """Sends a POST /start command to boot a configured node into OPERATIONAL state."""
    url = f"{BASE_URL}/start/{node_id}"
    try:
        log("ACTION", f"Sending NMT START to Node ID: {node_id}")
        response = requests.post(url, headers=HEADERS)
        if response.status_code == 200:
            msg = response.json().get("message", "Node in OPERATIONAL")
            log("SUCCESS", f"Node {node_id} Response: {msg}")
            return True
        log("FAILED", f"Gateway failed to start Node {node_id}. HTTP Status: {response.status_code}")
        return False
    except Exception as e:
        log("ERROR", f"Exception during start sequence for Node {node_id}: {e}")
        return False

def main():
    print("\n" + "="*60)
    print("   DYNAMIC LIVE CAN BUS AUTOMATION ENGINE    ")
    print("="*60)

    max_retries = 5
    retry_count = 0
    case = 0
    flashed_node_ids = set()

    while True:
        match case:
            case 0:
                log("STATE", "Entering Case 0: Checking for devices...")
                snapshot = Jump_to_preop()
                if not snapshot:
                    log("ERROR", "Failed to retrieve network snapshot, retrying in 2 seconds...")
                    time.sleep(2)
                    continue
                
                boot_nodes = snapshot.get("bootNodes", [])
                canopen_nodes = snapshot.get("canOpenNodes", [])
                
                # Print exact hardware states so status of every device is always visible
                print_exact_hardware_states(snapshot)
                
                # If no devices at all are detected on the CAN bus
                if not boot_nodes and not canopen_nodes:
                    retry_count += 1
                    if retry_count >= max_retries:
                        log("ERROR", f"No devices detected after {max_retries} scanning attempts. Exiting.")
                        break
                    log("SYSTEM", f"No devices detected. Retrying scan in 3 seconds (Attempt {retry_count}/{max_retries})...")
                    time.sleep(3)
                    continue
                    
                # If there are no bootnodes (all are configured/canOpenNodes)
                if not boot_nodes:
                    log("SYSTEM", "No devices left in bootmode. Moving to Case 1.")
                    case = 1
                    retry_count = 0  # Reset retry counter for Case 1
                    continue
                
                # Process only the first bootmode device to keep the network snapshot fresh
                node = boot_nodes[0]
                serial_number = node.get("serialNumber")
                serial_str = format_serial(serial_number)
                device_type = node.get("type", "Unknown device")
                
                print(f"\n--- Bootmode Device Detected ---")
                print(f"Status: Bootloader, Serial: {serial_str}, Type: {device_type}")
                
                # Calculate next available node ID, tracking flashed IDs locally to prevent conflicts
                existing_node_ids = {n.get("nodeID") for n in canopen_nodes if n.get("nodeID") is not None}
                all_assigned_ids = existing_node_ids.union(flashed_node_ids)
                current_node_id = 34
                while current_node_id in all_assigned_ids:
                    current_node_id += 2
                    
                log("ACTION", f"Setting device {serial_str} from bootmode to preoperational using flash...")
                if flash_device(serial_number, current_node_id, device_type):
                    log("SUCCESS", f"Device {serial_str} successfully transitioned to preoperational state with Node ID: {current_node_id}.")
                    flashed_node_ids.add(current_node_id)
                    time.sleep(1.5)
                else:
                    log("ERROR", f"Failed to flash device {serial_str}. Retrying in 2 seconds...")
                    time.sleep(2)
                
                # Loop back immediately to refresh the network state
                continue

            case 1:
                log("STATE", "Entering Case 1: Verifying preoperational state & transitioning to operational...")
                snapshot = Jump_to_preop()
                if not snapshot:
                    log("ERROR", "Failed to retrieve network snapshot, retrying in 2 seconds...")
                    time.sleep(2)
                    continue
                
                # Self-healing: if any devices are in bootmode, revert to Case 0 to flash/retry them
                boot_nodes = snapshot.get("bootNodes", [])
                if boot_nodes:
                    log("WARNING", f"Some devices ({len(boot_nodes)}) are still in bootmode. Reverting to Case 0 to flash them...")
                    case = 0
                    time.sleep(2)
                    continue
                    
                canopen_nodes = snapshot.get("canOpenNodes", [])
                preop_nodes = [n for n in canopen_nodes if n.get("state", "").upper() == "PREOPERATIONAL"]
                
                # If there are no preop nodes
                if not preop_nodes:
                    non_operational = [n for n in canopen_nodes if n.get("state", "").upper() != "OPERATIONAL"]
                    if non_operational:
                        log("WARNING", f"Found non-operational nodes: {non_operational}. Retrying state checks...")
                        time.sleep(2)
                        continue
                    log("SYSTEM", "All devices are already OPERATIONAL. Moving to Case 2.")
                    case = 2
                    retry_count = 0
                    continue
                
                # Display status of every device before transitioning
                log("SYSTEM", "Displaying status before transitioning to operational:")
                print_exact_hardware_states(snapshot)
                
                # Wait 2 seconds before starting transition
                time.sleep(2)
                
                # Transition all preoperational nodes to operational
                for node in preop_nodes:
                    node_id = node.get("nodeID")
                    serial_str = format_serial(node.get("serialNumber"))
                    device_type = node.get("type", "Unknown device")
                    log("ACTION", f"Transitioning preoperational Node ID {node_id} (s/n: {serial_str}) to operational...")
                    if start_device(node_id):
                        log("SUCCESS", f"Node {node_id} is now OPERATIONAL.")
                    else:
                        log("ERROR", f"Failed to transition Node {node_id} to OPERATIONAL.")
                
                # Show updated device status
                log("SYSTEM", "Showing updated device status after transition:")
                final_snapshot = Jump_to_preop()
                print_exact_hardware_states(final_snapshot)
                
                case = 2
                retry_count = 0
                continue

            case 2:
                log("STATE", "Entering Case 2: Final Verification of network state...")
                snapshot = Jump_to_preop()
                if not snapshot:
                    log("ERROR", "Failed to retrieve network snapshot.")
                    break
                    
                boot_nodes = snapshot.get("bootNodes", [])
                canopen_nodes = snapshot.get("canOpenNodes", [])
                
                # Check for any device in bootmode or not operational
                not_ready = [n for n in canopen_nodes if n.get("state", "").upper() != "OPERATIONAL"]
                
                if not boot_nodes and not not_ready:
                    print("\n" + "*"*50)
                    print("            READY FOR PUMPING            ")
                    print("*"*50 + "\n")
                    break
                else:
                    retry_count += 1
                    if retry_count >= max_retries:
                        log("ERROR", f"Verification failed. {len(boot_nodes)} devices in bootmode, {len(not_ready)} devices not operational. Exiting.")
                        break
                    log("WARNING", f"Verification failed. Retrying in 2 seconds (Attempt {retry_count}/{max_retries})...")
                    time.sleep(2)
                    continue

    print("=" *60)
    print("       Sequence Complete - Hardware done    ")



if __name__ == "__main__":
    main()