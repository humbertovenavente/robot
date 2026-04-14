#!/bin/bash
# Mata procesos previos
pkill -f orchestrator.py 2>/dev/null
pkill -f station.py 2>/dev/null
sleep 1

echo "Iniciando orquestador..."
python3 -m uvicorn orchestrator:app --host 0.0.0.0 --port 8000 &

sleep 2

echo "Iniciando 3 estaciones (deteccion de obstaculos ACTIVA)..."
python3 station.py --config station_config_1.yaml &
python3 station.py --config station_config_2.yaml &
python3 station.py --config station_config_3.yaml &

echo ""
echo "================================================"
echo "  Dashboard:  http://localhost:8000"
echo "  Obstaculos: ACTIVADO (yolov8n COCO)"
echo "  Si la camara detecta un objeto el robot"
echo "  se detiene y el dashboard muestra:"
echo "  '⚠ Objeto detectado: [nombre] — robot en espera'"
echo "================================================"
echo "Ctrl+C para detener todo"

# Espera y mata todo al salir
trap "pkill -f uvicorn; pkill -f station.py; echo 'Detenido.'" EXIT
wait
