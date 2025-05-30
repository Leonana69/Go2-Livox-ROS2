import time, os, math
import numpy as np
import threading, requests
from overrides import overrides
from PIL import Image
import cv2
import json
import queue
from enum import Enum
from scipy.spatial.transform import Rotation as R

import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist, PoseStamped
from sensor_msgs.msg import Image as ROSImage
from tf2_msgs.msg import TFMessage
from nav_msgs.msg import OccupancyGrid
from nav2_msgs.action import NavigateToPose
from rclpy.action import ActionClient
from action_msgs.msg import GoalStatus

from typego.robot_wrapper import RobotWrapper, RobotObservation
from typego.robot_info import RobotInfo
from typego.minispec_interpreter import MiniSpecProgram
from typego.yolo_client import YoloClient
from typego.skillset import SkillSet, SkillArg, SkillSetLevel
from typego.utils import quaternion_to_rpy, print_t, ImageRecover

from typego_interface.msg import WayPointArray

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))

GO2_CAM_K = np.array([
    [818.18507419, 0.0, 637.94628188],
    [0.0, 815.32431463, 338.3480119],
    [0.0, 0.0, 1.0]
], dtype=np.float32)

GO2_CAM_D = np.array([[-0.07203219],
                      [-0.05228525],
                      [ 0.05415833],
                      [-0.02288355]], dtype=np.float32)

def make_transform(translation, quaternion):
    T = np.eye(4)
    T[:3, :3] = R.from_quat(quaternion).as_matrix()
    T[:3, 3] = translation
    return T

class Go2Observation(RobotObservation):
    def __init__(self, robot_info: RobotInfo, node: Node, rate: int = 10):
        super().__init__(robot_info, rate)
        self.yolo_client = YoloClient(robot_info)
        self.image_receover = ImageRecover(GO2_CAM_K, GO2_CAM_D)
        self.init_ros_obs(node)

        self.map2odom_translation = np.array([0.0, 0.0, 0.0])
        self.map2odom_rotation = np.array([0.0, 0.0, 0.0, 1.0])
        self.odom2robot_translation = np.array([0.0, 0.0, 0.0])
        self.odom2robot_rotation = np.array([0.0, 0.0, 0.0, 1.0])

    def init_ros_obs(self, node: Node):
        # Subscribe to /camera/image_raw
        node.create_subscription(
            ROSImage,
            '/camera/image_raw',
            self._image_callback,
            10
        )

        # Subscribe to /tf
        node.create_subscription(
            TFMessage,
            '/tf',
            self._tf_callback,
            10
        )

        # Subscribe to /map
        node.create_subscription(
            OccupancyGrid,
            '/map',
            self._map_callback,
            10
        )

        node.create_subscription(
            WayPointArray,
            '/waypoints',
            self._waypoint_callback,
            10
        )

    def _image_callback(self, msg: ROSImage):
        # Convert ROS Image message to OpenCV image
        cv_image = np.frombuffer(msg.data, dtype=np.uint8).reshape(msg.height, msg.width, -1)
        # Undistort the image
        frame = self.image_receover.process(cv_image)
        self.image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))

    def _tf_callback(self, msg: TFMessage):
        # Extract position and orientation from the TF message
        for transform in msg.transforms:
            if transform.child_frame_id == "base_link":
                self.odom2robot_translation = np.array([
                    transform.transform.translation.x,
                    transform.transform.translation.y,
                    transform.transform.translation.z
                ])
                self.odom2robot_rotation = np.array([
                    transform.transform.rotation.x,
                    transform.transform.rotation.y,
                    transform.transform.rotation.z,
                    transform.transform.rotation.w
                ])
            elif transform.child_frame_id == "odom":
                self.map2odom_translation = np.array([
                    transform.transform.translation.x,
                    transform.transform.translation.y,
                    transform.transform.translation.z
                ])
                self.map2odom_rotation = np.array([
                    transform.transform.rotation.x,
                    transform.transform.rotation.y,
                    transform.transform.rotation.z,
                    transform.transform.rotation.w
                ])

        T_map_odom = make_transform(self.map2odom_translation, self.map2odom_rotation)
        T_odom_robot = make_transform(self.odom2robot_translation, self.odom2robot_rotation)
        T_map_robot = T_map_odom @ T_odom_robot
        self.position = T_map_robot[:3, 3]
        self.orientation = R.from_matrix(T_map_robot[:3, :3]).as_euler('xyz')  # or .as_quat()
        self.slam_map.update_robot_state((self.position[0], self.position[1]), self.orientation[2])
        # print_t(f"[Go2] Position: {self.position}, Orientation: {self.orientation}")

    def _map_callback(self, msg: OccupancyGrid):
        width = msg.info.width
        height = msg.info.height
        data = np.array(msg.data, dtype=np.int8).reshape((height, width))

        # Convert OccupancyGrid to grayscale image
        # map_data = np.zeros((height, width), dtype=np.uint8)
        # map_data[data == 0] = 255      # Free space = white
        # map_data[data == -1] = 128     # Unknown = gray
        # map_data[data > 0] = 0         # Occupied = black
        # map_data = cv2.flip(map_data, 0)

        # # Convert to Colored map_data
        # map_data = cv2.cvtColor(map_data, cv2.COLOR_GRAY2BGR)
        self.slam_map.update_map(data, width, height, (msg.info.origin.position.x, msg.info.origin.position.y), msg.info.resolution)
        # print_t(f"[Go2] Map size: {width}x{height}, Resolution: {msg.info.resolution}")

    def _waypoint_callback(self, msg: WayPointArray):
        self.slam_map.update_waypoints(msg)
        
    @overrides
    def _start(self):
        return
    
    @overrides
    def _stop(self):
        return
        
    @overrides
    async def process_image(self, image: Image.Image):
        await self.yolo_client.detect(image)
    
    @overrides
    def fetch_processed_result(self) -> tuple[Image.Image, list]:
        return self.yolo_client.latest_result

class Go2Posture(Enum):
    STANDING = "standing"
    LYING = "lying"
    MOVING = "moving"

    @staticmethod
    def from_string(s: str):
        if s == "standing":
            return Go2Posture.STANDING
        elif s == "lying":
            return Go2Posture.LYING
        elif s == "moving":
            return Go2Posture.MOVING
        else:
            raise ValueError(f"Unknown posture: {s}")
        
class Go2StateEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Go2Posture):
            return obj.value
        return super().default(obj)

class Go2Wrapper(RobotWrapper):
    def __init__(self, robot_info: RobotInfo, system_skill_func: list[callable]):
        self.init_ros_node()
        super().__init__(robot_info, Go2Observation(robot_info=robot_info, node=self.node), system_skill_func)

        self.running = True
        self.state = {
            "posture": Go2Posture.STANDING,
            "x": 0.0, "y": 0.0,
            "yaw": 0,
        }

        self.execution_queue = queue.Queue()
        self.active_program = None
        self.stop_action_event = threading.Event()

        extra = self.robot_info.extra
        if "url" not in extra:
            raise ValueError("Control url must be provided in extra")
        self.robot_url = extra["url"]
        response = requests.get(self.robot_url)
        if response.status_code != 200:
            raise RuntimeError(f"[Go2] Failed to connect to robot at {self.robot_url}")
        else:
            print_t(f"[Go2] {response.text}")

        self.ll_skillset.add_low_level_skill("stand_up", lambda: self._action('stand_up'), "Stand up")
        self.ll_skillset.add_low_level_skill("lying_down", lambda: self._action('stand_down'), "Stand down")
        # self.ll_skillset.add_low_level_skill("goto", self.goto, "Go to a specific position (x, y) in m", args=[SkillArg("x", float), SkillArg("y", float)])
        self.ll_skillset.add_low_level_skill("goto_waypoint", self.goto_waypoint, "Go to a way point", args=[SkillArg("id", int)])
        self.ll_skillset.add_low_level_skill("stop_action", self.stop_action, "Stop current action")
        self.ll_skillset.add_low_level_skill("look_object", self.look_object, "Look at an object", args=[SkillArg("object_name", str)])

        high_level_skills = [
            {
                "name": "scan",
                "definition": "{8{?is_visible($1){->True}rotate(45)}->False}",
                "description": "Rotate to find a specific object $1 when it's *not* in current view",
            },
            {
                "name": "scan_description",
                "definition": "{8{_1=probe($1);?_1!=False{->_1}rotate(45)}->False}",
                "description": "Rotate to find an abstract object $1 when it's *not* in current view",
            },
            {
                "name": "orienting",
                "definition": "{_1=object_x($1);rotate((0.5-_1)*80)}",
                "description": "Rotate to align with object $1",
            },
            {
                "name": "goto_object",
                "definition": "2{orienting($1);_1=object_distance($1)/2;{move(_1, 0)}}",
                "description": "Move to object $1 in the view (orienting then go forward)"
            }
        ]

        self.hl_skillset = SkillSet(SkillSetLevel.HIGH, self.ll_skillset)
        for skill in high_level_skills:
            self.hl_skillset.add_high_level_skill(skill['name'], skill['definition'], skill['description'])

        self.execution_thread = threading.Thread(target=self.worker)
        self.execution_thread.start()

    def init_ros_node(self):
        rclpy.init()
        self.node = rclpy.create_node('go2')
        # Subscribe to /camera/image_raw
        self.node.create_subscription(
            Twist,
            '/cmd_vel',
            self._cmd_vel_callback,
            10
        )

        self.navigate_client = ActionClient(self.node, NavigateToPose, 'navigate_to_pose')

    def _cmd_vel_callback(self, msg: Twist):
        if msg.linear.x == 0.0 and msg.linear.y == 0.0 and msg.angular.z == 0.0:
            return
        control = {
            'command': 'nav',
            'vx': msg.linear.x,
            'vy': msg.linear.y,
            'vyaw': msg.angular.z
        }

        self._send_control(control)

    def append_action(self, action: str):
        """
        Appends an action to the execution queue.
        """
        if action == "keep()":
            return
        elif action == "stop_action()":
            self.stop_action()
            self.memory.execute_action("stop_action()")
            return
        elif action == "done(True)":
            self.memory.end_subtask(True)
            return
        elif action == "done(False)":
            self.memory.end_subtask(False)
            return

        self.execution_queue.put(action)

    def worker(self):
        while self.running:
            if not self.execution_queue.empty():
                action = self.execution_queue.get()
                print_t(f"[Go2] Executing action: {action}")
                self.active_program = MiniSpecProgram(self, None)
                self.active_program.parse(action)
                self.active_program.eval()
            time.sleep(0.1)

    @overrides
    def start(self) -> bool:
        self.spin_thread = threading.Thread(target=rclpy.spin, args=(self.node,))
        self.spin_thread.start()
        self.observation.start()
        return True

    @overrides
    def stop(self):
        self.running = False
        self.execution_thread.join()
        self.observation.stop()
        if self.spin_thread is not None:
            rclpy.shutdown()
            self.spin_thread.join()

    @overrides
    def get_state(self) -> str:
        js = {
            "time": time.strftime("%H:%M:%S", time.localtime(time.time())),
            "posture": self.state["posture"].value,
            "waypoint_id": self.observation.slam_map.get_nearest_waypoint_id((self.observation.position[0], self.observation.position[1])),
        }
        return json.dumps(js)

    def stop_action(self):
        print_t("[Go2] Stopping action...")
        self.stop_action_event.set()
        if self.active_program:
            self.active_program.stop()
        time.sleep(0.5)
        self.stop_action_event.clear()

    def look_object(self, object_name: str):
        current_pitch = 0
        current_yaw = 0

        while not self.stop_action_event.is_set():
            info = self.get_obj_info(object_name)

            if info is None:
                break

            dx = 0.5 - info.x
            dy = info.y - 0.5

            # Dead zone in x-direction
            if abs(dx) > 0.05:
                current_yaw += dx / 4.0
            # Dead zone in y-direction (optional: adjust based on your use case)
            if abs(dy) > 0.05:
                current_pitch += dy / 6.0
                current_pitch = max(-0.75, min(0.75, current_pitch))

            # If yaw exceeds limits, rotate and skip the Euler update
            if abs(current_yaw) > 0.6:
                self.rotate(current_yaw * 180.0 / math.pi / 4)
                current_yaw = 0
                continue

            self._action("euler", roll=0, pitch=current_pitch, yaw=current_yaw)
            time.sleep(0.1)

    def _action(self, action: str, **args):
        control = {
            "command": action,
            "timeout": 3.0,
            **args
        }

        match action:
            case "stand_up":
                self.state["posture"] = Go2Posture.STANDING
            case "stand_down":
                self.state["posture"] = Go2Posture.LYING
            case _:
                pass

        self._send_control(control)

    def _send_control(self, control: dict):
        response = requests.post(
            self.robot_url + "control",
            json=control,
            headers={"Content-Type": "application/json"}
        )
        if response.status_code != 200:
            print_t(f"[Go2] Failed to send command: {response.text}")

    def _move(self, linear_x: float=0.0, linear_y: float=0.0, angular_z: float=0.0, duration: float=3.0):
        """
        Helper function to publish Twist messages for a specified duration.
        """
        control = { "timeout": duration }
        if linear_x != 0.0 or linear_y != 0.0:
            control["command"] = "move"
            control["dx"] = linear_x
            control["dy"] = linear_y
            control["body_frame"] = True
        elif angular_z != 0.0:
            control["command"] = "rotate"
            control["delta_rad"] = angular_z
        self._send_control(control)

    @overrides
    def move(self, dx: float, dy: float) -> bool:
        """
        Moves the robot by the specified distance in the x (forward/backward) and y (left/right) directions.
        """
        print(f"-> Move by ({dx}, {dy}) m")
        self.state["posture"] = Go2Posture.MOVING
        self._move(linear_x=dx, linear_y=dy, duration=5.0)
        self.state["posture"] = Go2Posture.STANDING
        return True

    @overrides
    def rotate(self, deg: float) -> bool:
        """
        Rotates the robot by the specified angle in degrees.
        """
        print(f"-> Rotate by {deg} degrees")
        self.state["posture"] = Go2Posture.MOVING
        self._move(angular_z=math.radians(deg), duration=5.0)
        self.state["posture"] = Go2Posture.STANDING
        return True
    
    def goto_waypoint(self, id: int) -> bool:
        print(f"-> Go to waypoint {id}")
        wp = self.observation.slam_map.get_waypoint(id)
        if wp is None:
            print_t(f"Waypoint {id} not found")
            return False
        return self.goto(wp.x, wp.y)
    
    def goto(self, x: float, y: float, timeout_sec: float = 30.0) -> bool:
        print(f"-> Go to ({x}, {y})")
        self.state["posture"] = Go2Posture.MOVING

        goal_msg = NavigateToPose.Goal()
        goal_msg.pose.header.frame_id = 'map'
        goal_msg.pose.header.stamp = self.node.get_clock().now().to_msg()
        goal_msg.pose.pose.position.x = x
        goal_msg.pose.pose.position.y = y
        goal_msg.pose.pose.orientation.z = 0.0
        goal_msg.pose.pose.orientation.w = 1.0

        self.navigate_client.wait_for_server()

        done_event = threading.Event()
        result_status = {"status": None}
        goal_handle_container = {}

        def goal_response_callback(future):
            goal_handle = future.result()
            if not goal_handle.accepted:
                print_t("Navigation: Goal rejected")
                result_status["status"] = False
                done_event.set()
                return

            goal_handle_container["handle"] = goal_handle  # Save for timeout/cancel
            result_future = goal_handle.get_result_async()

            def result_callback(result_future):
                result = result_future.result()
                goal_id = goal_handle.goal_id
                if result.status == GoalStatus.STATUS_SUCCEEDED:  # Use enum for clarity
                    print_t("Navigation: Goal succeeded")
                    result_status["status"] = True
                else:
                    print_t(f"Navigation: Goal failed with status {result.status}")
                    result_status["status"] = False
                done_event.set()

            result_future.add_done_callback(result_callback)

        send_goal_future = self.navigate_client.send_goal_async(goal_msg)
        send_goal_future.add_done_callback(goal_response_callback)

        # completed_in_time = done_event.wait(timeout=timeout_sec)
        # if not completed_in_time:
        #     print_t("Navigation: Timeout — cancelling goal...")
        #     goal_handle = goal_handle_container.get("handle")
        #     if goal_handle and goal_handle.accepted:
        #         goal_handle.cancel_goal_async()
        #     self.state["posture"] = Go2Posture.STANDING
        #     return False

        start_time = time.time()
        while not done_event.is_set():
            if self.stop_action_event.is_set() or time.time() - start_time > timeout_sec:
                print_t("Navigation: Stopped")
                break
            time.sleep(0.1)

        if not done_event.is_set():
            goal_handle = goal_handle_container.get("handle")
            if goal_handle and goal_handle.accepted:
                goal_handle.cancel_goal_async()
            result_status["status"] = False

        self.state["posture"] = Go2Posture.STANDING
        return result_status["status"]