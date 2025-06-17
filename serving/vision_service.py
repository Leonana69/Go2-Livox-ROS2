from quart import Quart, request
import os, json, sys

PROJ_DIR = os.environ.get("PROJ_PATH", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ_DIR)
from service_manager import ServiceManager

sys.path.append(os.path.join(PROJ_DIR, "proto"))
import hyrch_serving_pb2
import hyrch_serving_pb2_grpc

app = Quart(__name__)
grpcServiceManager = ServiceManager()

SERVICE_INFO = [
    # { "name": "yolo", "host": "localhost", "port" : [50050, 50051] },
    # { "name": "yolo3d", "host": "localhost", "port" : [50060] },
    # { "name": "clip", "host": "localhost", "port" : [50052] },
    { "name": "vlm", "host": "localhost", "port" : [50054] },
]

@app.before_serving
async def before_serving():
    for service in SERVICE_INFO:
        grpcServiceManager.add_service(service["name"], service["host"], service["port"])
    await grpcServiceManager._initialize_channels()

@app.route('/process', methods=['POST'])
async def process():
    form = await request.form
    json_str = form.get('json_data')

    if not json_str:
        return {"error": "Missing json data"}, 400

    try:
        json_data = json.loads(json_str)
        robot_info = json_data["robot_info"]
        service_type = json_data["service_type"]

        if service_type == "yolo" or service_type == "yolo3d" or service_type == "clip" or service_type == "vlm":
            files = await request.files
            image_data = files['image']
            image_bytes = image_data.read()

    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}, 400

    channel = await grpcServiceManager.get_service_channel(service_type, robot_info)

    if isinstance(channel, str):
        return {"error": f"Channel error: {channel}"}, 400

    if service_type == "yolo" or service_type == "yolo3d":
        stub = hyrch_serving_pb2_grpc.YoloServiceStub(channel)
        response = await stub.Detect(hyrch_serving_pb2.DetectRequest(
            json_data=json_str,
            image_data=image_bytes
        ))
    elif service_type == "clip":
        stub = hyrch_serving_pb2_grpc.ClipServiceStub(channel)
        response = await stub.Detect(hyrch_serving_pb2.DetectRequest(
            json_data=json_str,
            image_data=image_bytes
        ))
    elif service_type == "vlm":
        stub = hyrch_serving_pb2_grpc.VLMServiceStub(channel)
        response = await stub.Detect(hyrch_serving_pb2.DetectRequest(
            json_data=json_str,
            image_data=image_bytes
        ))

    return response.json_data
