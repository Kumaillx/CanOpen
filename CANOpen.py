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
    serial_str = format_serial(serial)
    url = f"{BASE_URL}/flash/{serial_str}/{node_id}"
    try:
        log("ACTION", f"Flashing Serial: {serial_str} ({device_type}) -> Assigning Node ID: {node_id}")
        # Add delay to let the gateway and hardware stabilize before flash
        time.sleep(1.5)
        response = requests.post(url, headers=HEADERS)
        if response.status_code in [200, 201]:
            log("SUCCESS", f"Node {node_id} identity stored successfully.")
            # Add delay to let the device initialize after flashing
            time.sleep(1.5)
            return True
        log("FAILED", f"Gateway rejected flash for Serial {serial_str}. HTTP Status: {response.status_code}")
        return False
    except Exception as e:
        log("ERROR", f"Exception during flash sequence for Serial {serial_str}: {e}")
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

    # 1. Fetch initial live topology mapping from GSShell
    log("SYSTEM", "Querying /what to find physical hardware footprints...")
    initial_snapshot = Jump_to_preop()
    if not initial_snapshot:
        return
    # Print out what the network looks like right now
    print_exact_hardware_states(initial_snapshot)

    # Extract the live arrays from the snapshot
    boot_nodes = initial_snapshot.get("bootNodes", [])
    canopen_nodes = initial_snapshot.get("canOpenNodes", [])

    if not boot_nodes and not canopen_nodes:
        log("SYSTEM", "No devices detected on the CAN bus.")
        return

    # Determine existing node IDs to prevent assignment conflicts
    existing_node_ids = {node.get("nodeID") for node in canopen_nodes if node.get("nodeID") is not None}
    current_node_id = 34
    while current_node_id in existing_node_ids:
        current_node_id += 2

    # 2. Interactive Flashing and Starting Loop for Bootloader devices
    if boot_nodes:
        log("SYSTEM", f"Captured {len(boot_nodes)} unconfigured devices floating on the wire.")
        for node in boot_nodes:
            serial_number = node.get("serialNumber")
            serial_str = format_serial(serial_number)
            device_type = node.get("type", "Unknown device")

            print(f"\n--- Device {serial_str} ({device_type}) ---")
            print("Status: Bootloader")
            user_choice = input(f"Do you want to flash this device to make it operational? (y/n): ").strip().lower()
            
            if user_choice in ['yes', 'y']:
                node_id = current_node_id
                # Prepare current_node_id for next device
                current_node_id += 2
                while current_node_id in existing_node_ids:
                    current_node_id += 2

                # Flash the device to assign Node ID
                if flash_device(serial_number, node_id, device_type):
                    # Show genuine state (which is now PREOPERATIONAL)
                    print(f"Device genuine state: PREOPERATIONAL (Assigned Node ID: {node_id})")
                    start_choice = input(f"Do you wanna start Node {node_id}? (y/n): ").strip().lower()
                    if start_choice in ['yes', 'y']:
                        if start_device(node_id):
                            print(f"Node {node_id} is now OPERATIONAL.")
                        else:
                            print(f"Failed to start Node {node_id}. State remains PREOPERATIONAL.")
                    else:
                        print(f"Node {node_id} state remains PREOPERATIONAL.")
                else:
                    print(f"Failed to flash device {serial_str}.")
            else:
                print(f"Skipping device {serial_str}. State remains Bootloader.")
                time.sleep(0.1)

    # 3. Interactive Starting Loop for already configured devices
    if canopen_nodes:
        log("SYSTEM", f"Checking {len(canopen_nodes)} configured devices...")
        for node in canopen_nodes:
            node_id = node.get("nodeID")
            state = node.get("state", "UNKNOWN").upper()
            serial_number = node.get("serialNumber")
            serial_str = format_serial(serial_number)
            device_type = node.get("type", "Unknown device")

            print(f"\n--- CANopen Node {node_id} (s/n: {serial_str}, {device_type}) ---")
            print(f"Genuine state: {state}")
            
            if state == "PREOPERATIONAL":
                start_choice = input(f"Do you wanna start Node {node_id}? (y/n): ").strip().lower()
                if start_choice in ['yes', 'y']:
                    if start_device(node_id):
                        print(f"Node {node_id} is now OPERATIONAL.")
                    else:
                        print(f"Failed to start Node {node_id}. State remains PREOPERATIONAL.")
                else:
                    print(f"Node {node_id} state remains PREOPERATIONAL.")
            elif state == "OPERATIONAL":
                print(f"Node {node_id} is already OPERATIONAL. No action needed.")
            else:
                print(f"Node {node_id} is in state: {state}. No actions available.")
            time.sleep(0.1)

    # 4. Show final snapshot
    print("\n" + "="*60)
    log("SYSTEM", "Querying /what for the final hardware states...")
    final_snapshot = Jump_to_preop()
    print_exact_hardware_states(final_snapshot)

    print("=" *60)
    print("       Sequence Complete - Hardware done    ")



if __name__ == "__main__":
    main()