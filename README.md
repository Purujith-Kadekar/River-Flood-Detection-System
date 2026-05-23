# River Flood Detection System

A real-time flood monitoring and alerting system using LoRa technology.

## Project Structure

- **src/sensor/**: ESP32 source code (C++/Arduino) for the water level sensor node.
- **src/gateway/**: Raspberry Pi gateway code (Python) to receive LoRa data and log to a database.
- **src/dashboard/**: Flask-based web application to visualize water levels and flood history.
- **docs/**: Research papers, pinout diagrams, and system documentation.
- **assets/**: Project images and component photos.
- **archive/**: Legacy code and older versions.

## How to Run

1.  **Gateway:**
    ```bash
    cd src/gateway
    # Original version (use this if currently running):
    python3 receiver.py
    
    # Updated version (pre-configured for new project structure):
    python3 receiver_v2.py
    ```
2.  **Dashboard:**
    ```bash
    cd src/dashboard
    python3 app.py
    ```
3.  **Sensor Node:**
    Flash the ESP32 using PlatformIO or Arduino IDE with the code in `src/sensor`.

## Key Changes in v2
- **Path Resolution:** `receiver_v2.py` is updated to automatically find the dashboard's database in `src/dashboard/instance/` or `dashboard/instance/`, making it easier to run from the root or the gateway folder.
- **Project Structure:** Code is now organized into `src/sensor`, `src/gateway`, and `src/dashboard`.
