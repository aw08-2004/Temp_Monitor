import requests
import time
import wmi  # <-- Added missing import

# ================================
# CONFIG
# ================================
# Use HTTPS endpoint (HTTP gets redirected and can turn POST into GET)
HUB_URL = "https://temp.arkeanos.net/api/report"
MACHINE_NAME = "FCOM1109"
INTERVAL = 5  # seconds

# ================================
# TEMP READ
# ================================
def get_cpu_temp():
    """Reads the CPU temperature using OpenHardwareMonitor."""
    try:
        w = wmi.WMI(namespace="root\\OpenHardwareMonitor")
        sensors = w.Sensor()
        
        for sensor in sensors:
            if sensor.SensorType == u'Temperature' and "CPU Package" in sensor.Name:
                return round(sensor.Value, 1)
                
        for sensor in sensors:
            if sensor.SensorType == u'Temperature' and "CPU" in sensor.Name:
                return round(sensor.Value, 1)
                
        for sensor in sensors:
            if sensor.SensorType == u'Temperature':
                return round(sensor.Value, 1)
                
    except Exception as e:
        print(f"Error reading local temp: {e}")
        
    return None

# ================================
# MAIN LOOP
# ================================
if __name__ == "__main__":
    print(f"Starting companion monitor for {MACHINE_NAME}...")
    print(f"Sending data to {HUB_URL} every {INTERVAL} seconds.")
    
    while True:
        current_temp = get_cpu_temp()
        print(current_temp)
        
        if current_temp is not None:
            try:
                # Send the POST request to the central hub
                response = requests.post(
                    HUB_URL, 
                    json={"machine": MACHINE_NAME, "temp": current_temp},
                    timeout=3, # Good practice: don't let it hang forever if the hub is offline
                    allow_redirects=False
                )
                print(f"Sent: {current_temp}°C - Hub responded: {response.status_code}")
            except requests.exceptions.RequestException:
                # Catch connection errors cleanly so it doesn't crash if the hub is rebooting
                print("Failed to connect to Hub. Retrying next cycle...")
        
        time.sleep(INTERVAL)
