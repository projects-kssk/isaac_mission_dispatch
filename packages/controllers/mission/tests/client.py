"""
SPDX-FileCopyrightText: NVIDIA CORPORATION & AFFILIATES
Copyright (c) 2021-2022 NVIDIA CORPORATION & AFFILIATES. All rights reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    https://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

SPDX-License-Identifier: Apache-2.0
"""

# This repository implements data types and logic specified in the VDA5050 protocol, which is
# specified here https://github.com/VDA5050/VDA5050/blob/main/VDA5050_EN.md
import argparse
import json
import re
import socket
import time
from typing import List, Optional

import paho.mqtt.client as mqtt_client
import pydantic

import packages.controllers.mission.vda5050_types as types

DISTANCE_THRESHOLD = 0.05

# How long to wait in seconds before trying to reconnect to the mqtt broker
MQTT_RECONNECT_PERIOD = 0.5

class ActionServer:
    """Represents an action server that executes action and sends feedbacks"""
    def __init__(self, robot_name):
        self._status = types.VDA5050ActionStatus.WAITING
        self._should_fail = False
        self._completed_time = float('inf')
        self._robot_name = robot_name

    @property
    def triggered(self):
        return self._status != types.VDA5050ActionStatus.WAITING

    @property
    def finished(self):
        return self._status == types.VDA5050ActionStatus.FINISHED

    @property
    def failed(self):
        return self._status == types.VDA5050ActionStatus.FAILED

    @property
    def get_status(self):
        return self._status

    def start(self, should_fail: bool = False, execution_time: float = 1):
        # Determine the time when the mission will fail/finish
        self._should_fail = should_fail
        self._completed_time = time.time() + execution_time
        self._status = types.VDA5050ActionStatus.RUNNING

    def update_status(self):
        # If this has been running for at least execution_time
        if time.time() < self._completed_time:
            self._status = types.VDA5050ActionStatus.RUNNING
        else:
            if self._should_fail:
                self._status = types.VDA5050ActionStatus.FAILED
            else:
                self._status = types.VDA5050ActionStatus.FINISHED
        return self._status

    def reset(self):
        self._status = types.VDA5050ActionStatus.WAITING


class RobotInit:
    """Represents the initial state of a robot in the simulation"""
    def __init__(self, name: str, x: float, y: float, theta: float = 0.0, failure_period: int = 0,
                 manufacturer: str = "", serial_number: str = ""):
        self.name = name
        self.x = x
        self.y = y
        self.theta = theta
        self.failure_period = failure_period
        self.manufacturer = manufacturer
        self.serial_number = serial_number

    def __str__(self) -> str:
        params = [self.name, self.x, self.y, self.theta, self.failure_period]
        if self.manufacturer != "":
            params.append(self.manufacturer)
        if self.serial_number != "":
            params.append(self.serial_number)
        return ",".join(str(param) for param in params)


class Robot:
    """Represents and handles the movement of a simulated robot"""
    def __init__(self, init: RobotInit, client: mqtt_client.Client, speed: float,
                 tick_period: float = 0.25, mqtt_prefix: str = "uagv/v1"):
        self.name = init.name
        self.order: Optional[types.VDA5050Order] = None
        self.state = types.VDA5050OrderInformation(
            headerId=0,
            timestamp="",
            manufacturer=init.manufacturer,
            serialNumber=init.serial_number,
            orderId="",
            orderUpdateId=0,
            lastNodeId="",
            lastNodeSequenceId=0,
            nodeStates=[],
            edgeStates=[],
            agvPosition={"x": init.x, "y": init.y, "theta": init.theta, "mapId": "map01"},
            actionStates=[])
        self.client = client
        self.failure_period = init.failure_period
        self.time_to_next_failure = 0
        self.speed = speed
        self.tick_period = tick_period
        self._current_node = 0
        self._current_action_id = 0
        self._action_server = ActionServer(self.name)
        self._mqtt_prefix = mqtt_prefix

    def publish_state(self):
        self.client.publish(f"{self._mqtt_prefix}/{self.name}/state", self.state.json())

    def move(self, target_node: types.VDA5050Node):
        if target_node.nodePosition is not None:
            # Are we still moving in the X direction?
            if abs(target_node.nodePosition.x - self.state.agvPosition.x) >= DISTANCE_THRESHOLD:
                direction = 1 if target_node.nodePosition.x > self.state.agvPosition.x else -1
                distance = min(abs(self.state.agvPosition.x - target_node.nodePosition.x),
                               self.speed * self.tick_period)
                self.state.agvPosition.x += direction * distance
                return True

            # Are we still moving in the Y direction?
            if abs(target_node.nodePosition.y - self.state.agvPosition.y) >= DISTANCE_THRESHOLD:
                direction = 1 if target_node.nodePosition.y > self.state.agvPosition.y else -1
                distance = min(abs(self.state.agvPosition.y - target_node.nodePosition.y),
                               self.speed * self.tick_period)
                self.state.agvPosition.y += direction * distance
                return True

        # We have reached the target node
        if self._current_action_id == 0 and not self._action_server.triggered:
            self.info(f"Reached node {target_node.nodeId}")
            self.state.nodeStates.pop(0)
            self.state.lastNodeSequenceId = target_node.sequenceId
            self.state.lastNodeId = target_node.nodeId
            if self.state.edgeStates:
                self.state.edgeStates.pop(0)
        return False

    def execute_order(self):
        # Do nothing if there is no order
        if self.order is None:
            return

        # Do nothing if we have already completed the current order
        if self._current_node >= len(self.order.nodes):
            return

        # Do nothing if this mission has already failed
        if self.state.errors:
            return

        # Fail if we have reached the failure period
        if self.time_to_next_failure == 0 and self.failure_period > 0:
            self.info(f"failed mission {self.order.orderId}")
            self.state.errors.append(
                types.VDA5050Error(errorReferences=\
                    [types.VDA5050ErrorReference(
                        referenceKey="node_id",
                        referenceValue=self.order.nodes[self._current_node].nodeId)],
                                   errorDescription="Failure period reached"))
            return

        # Get the next node we are trying to get to
        target_node = self.order.nodes[self._current_node]
        if self.move(target_node):
            return

        # Check if this node contains actions
        if self._current_action_id < len(target_node.actions):
            target_action = target_node.actions[self._current_action_id]
            if self._action_server.triggered:
                self._action_server.update_status()
                if self._action_server.finished:
                    self.info(f"Finished action {target_action.actionId}")
                    self._current_action_id += 1
                    self.update_action_state(target_action.actionId,
                                             types.VDA5050ActionStatus.FINISHED)
                    self._action_server.reset()
                else:
                    if self._action_server.failed:
                        self.state.errors.append(
                            types.VDA5050Error(errorReferences=\
                                [types.VDA5050ErrorReference(
                                    referenceKey="action_id",
                                    referenceValue=target_action.actionId)],
                                               errorDescription="Action failure"))
                        self._action_server.reset()
                    self.update_action_state(target_action.actionId,
                                             self._action_server.get_status)
            else:
                if target_action.actionParameters is not None:
                    self._action_server.start(json.loads(target_action.actionParameters[0].value),
                                              float(target_action.actionParameters[1].value))
                    self.info(f"Started action {target_action.actionId}")
                else:
                    self._action_server.start(should_fail=True)
                    self.info(f"Action {target_action.actionId} failed due to lack of parameters")

        else:
            self._current_action_id = 0
            self._current_node += 1

    def update_action_state(self, action_id, action_status):
        for action_state in self.state.actionStates:
            if action_state.actionId == action_id:
                action_state.actionStatus = action_status
                break

    def update(self):
        self.execute_order()
        self.publish_state()


    def send_order(self, order: types.VDA5050Order):
        self.info(f"Got order {order.orderId}")
        new_order = True
        if self.order is not None:
            new_order = self.order.orderId != order.orderId
        self.order = order

        self.state.orderId = self.order.orderId
        self.state.orderUpdateId = self.order.orderUpdateId
        self.state.headerId = self.order.headerId
        self.state.timestamp = self.order.timestamp
        self.state.version = self.order.version

        # If this is a new order, reset all the node states & edge states
        if new_order:
            if self.failure_period:
                self.time_to_next_failure = \
                    (self.time_to_next_failure - 1) % self.failure_period
            self.state.errors = []
            self.state.nodeStates = []
            self.state.actionStates = []
            for node in self.order.nodes:
                self.state.nodeStates += [node.to_node_state()]
                self.state.actionStates += [types.VDA5050ActionState(actionId=action.actionId)
                                            for action in node.actions]
            self.state.edgeStates = [edge.to_edge_state() for edge in self.order.edges]
            self._current_node = 0
            self._current_action_id = 0
            self.state.lastNodeId = ""
        self.publish_state()

    def info(self, message: str):
        print(f"[Isaac Mission Dispatch Client Simulator] | INFO: "
              f"[{self.name}] {message}", flush=True)

def robot_parser(spec: str) -> RobotInit:
    params = spec.split(",")
    theta, failure_period, manufacturer, serial_number = "0", "0", "", ""
    if len(params) == 3:
        name, x, y = params
    elif len(params) == 4:
        name, x, y, theta = params
    elif len(params) == 5:
        name, x, y, theta, failure_period = params
    elif len(params) == 7:
        name, x, y, theta, failure_period, manufacturer, serial_number = params
    else:
        raise argparse.ArgumentTypeError("""Robot spec must be of the form \"name,x,y\",
                                         \"name,x,y,theta\", \"name,x,y,theta,failure_period\", or
                                         \"name,x,y,theta,failure_period, manufacturer, 
                                         serial_number\"""")
    return RobotInit(name, float(x), float(y), float(theta), int(failure_period),
                     manufacturer, serial_number)


class Simulator:
    """Initialies robot objects, drives their state, and reacts to orders"""
    def __init__(self, robots: List[RobotInit], speed: float, mqtt_host: str = "localhost",
                 mqtt_transport: str = "tcp", mqtt_ws_path: Optional[str] = None,
                 mqtt_port: int = 1883, mqtt_prefix: str = "uagv/v1", tick_period: float = 0.25):
        self.mqtt_prefix = mqtt_prefix
        self.client = self._connect_to_mqtt(mqtt_host, mqtt_port, mqtt_transport, mqtt_ws_path)
        self.client.loop_start()
        self.robots = {init.name: Robot(init, self.client, speed, tick_period, mqtt_prefix)
                       for init in robots}
        self.tick_period = tick_period

    def _connect_to_mqtt(self, host: str, port: int, transport: str, ws_path: Optional[str]) \
                         -> mqtt_client.Client:
        client = mqtt_client.Client(transport=transport)
        if transport == "websockets" and ws_path is not None:
            client.ws_set_options(path=ws_path)
        client.on_connect = self._mqtt_on_connect
        client.on_message = self._mqtt_on_message
        connected = False
        while not connected:
            try:
                client.connect(host, port)
                connected = True
            except ConnectionRefusedError:
                print("Failed to connect to mqtt broker, retrying in "
                      f"{MQTT_RECONNECT_PERIOD}s")
                time.sleep(MQTT_RECONNECT_PERIOD)
            except socket.gaierror:
                print(f"Could not resolve mqtt hostname {host}, retrying in "
                      f"{MQTT_RECONNECT_PERIOD}s")
                time.sleep(MQTT_RECONNECT_PERIOD)
        return client

    def _mqtt_on_connect(self, client, userdata, flags, rc):
        client.subscribe(f"{self.mqtt_prefix}/+/order")

    def _mqtt_on_message(self, client, userdata, msg):
        # Determine which robot the order belongs to
        match = re.match(f"{self.mqtt_prefix}/(.*)/order", msg.topic)
        if match is None:
            print(f"WARNING: Got message from unrecognized topic \"{msg.topic}\"", flush=True)
            return
        robot = match.groups()[0]

        # Ignore unkown robots
        if robot not in self.robots:
            print(f"WARNING: Got order for unrecognized robot \"{robot}\"", flush=True)
            return

        # Attempt to decode and use the message
        try:
            self.robots[robot].send_order(types.VDA5050Order(**json.loads(msg.payload)))
        except (json.decoder.JSONDecodeError, pydantic.error_wrappers.ValidationError) as error:
            print(f"WARNING: Ignoring badly formed message: {error}", flush=True)


    def run(self):
        while True:
            time.sleep(self.tick_period)
            for robot in self.robots.values():
                robot.update()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--robots", nargs="+", type=robot_parser, required=True)
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--mqtt_host", default="localhost",
                        help="The hostname of the mqtt server to connect to")
    parser.add_argument("--mqtt_port", default=1883, type=int,
                        help="The port of the mqtt server to connect to")
    parser.add_argument("--mqtt_transport", default="tcp", choices=("tcp", "websockets"),
                        help="Set transport mechanism as websockets or raw tcp")
    parser.add_argument("--mqtt_ws_path", default=None,
                        help="The path for the websocket if 'mqtt_transport' is 'websockets'")
    parser.add_argument("--mqtt_prefix", default="uagv/v1",
                        help="The MQTT topic prefix")
    parser.add_argument("--tick_period", default=0.25, type=float,
                        help="The tick period of the simulator")
    args = parser.parse_args()

    sim = Simulator(**vars(args))
    sim.run()
