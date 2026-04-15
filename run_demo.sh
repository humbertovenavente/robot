#!/bin/bash
# Mata procesos previos
pkill -f orchestrator.py 2>/dev/null
pkill -f station.py     2>/dev/null
pkill -f web_nav.py     2>/dev/null
pkill -f uvicorn        2>/dev/null
lsof -ti:8080 | xargs kill -9 2>/dev/null
lsof -ti:8000 | xargs kill -9 2>/dev/null
sleep 2

echo "Iniciando orquestador..."
python3 -m uvicorn orchestrator:app --host 0.0.0.0 --port 8000 &

sleep 2

echo "Iniciando 3 estaciones (deteccion de obstaculos ACTIVA)..."
python3 station.py --config station_config_1.yaml &
python3 station.py --config station_config_2.yaml &
python3 station.py --config station_config_3.yaml &

sleep 1

echo "Iniciando Bug Navigator (camara en robot -> sigue QR)..."
python3 web_nav.py --port 8080 &

echo ""
echo "================================================"
echo "  Dashboard estaciones: http://localhost:8000"
echo "  Bug Navigator (web):  http://localhost:8080"
echo ""
echo "  El bug busca un QR con su camara:"
echo "    searching   -> girando buscando QR"
echo "    centering   -> alineandose al QR"
echo "    approaching -> avanzando hacia el QR"
echo "    arrived     -> llego al QR, se detiene"
echo "================================================"
echo "Ctrl+C para detener todo"

trap "pkill -f uvicorn; pkill -f station.py; pkill -f web_nav.py; echo 'Detenido.'" EXIT
wait
