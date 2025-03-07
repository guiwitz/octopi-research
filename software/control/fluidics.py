import pandas as pd
import threading
from typing import Dict, Optional

from fluidics_v2.software.control.controller import FluidControllerSimulation, FluidController
from fluidics_v2.software.control.syringe_pump import SyringePumpSimulation, SyringePump
from fluidics_v2.software.control.selector_valve import SelectorValveSystem
from fluidics_v2.software.control.disc_pump import DiscPump
from fluidics_v2.software.control.temperature_controller import TCMControllerSimulation, TCMController
from fluidics_v2.software.merfish_operations import MERFISHOperations
from fluidics_v2.software.open_chamber_operations import OpenChamberOperations
from fluidics_v2.software.experiment_worker import ExperimentWorker
from fluidics_v2.software.control._def import CMD_SET

import json


class Fluidics:
    def __init__(
        self, config_path: str, sequence_path: str, simulation: bool = False, callbacks: Optional[Dict] = None
    ):
        """Initialize the fluidics runner

        Args:
            config_path: Path to the configuration JSON file
            sequence_path: Path to the sequence CSV file
            simulation: Whether to run in simulation mode
            callbacks: Optional dictionary of callback functions
        """
        self.config_path = config_path
        self.sequence_path = sequence_path
        self.simulation = simulation
        self.port_list = None

        # Initialize member variables
        self.config = None
        self.sequences = None
        self.controller = None
        self.syringe_pump = None
        self.selector_valve_system = None
        self.disc_pump = None
        self.temperature_controller = None
        self.experiment_ops = None
        self.worker = None
        self.thread = None

        # Set default callbacks if none provided
        self.callbacks = callbacks or {
            "update_progress": lambda idx, seq_num, status: print(f"Sequence {idx} ({seq_num}): {status}"),
            "on_error": lambda msg: print(f"Error: {msg}"),
            "on_finished": lambda: print("Experiment completed"),
            "on_estimate": lambda time, n: print(f"Est. time: {time}s, Sequences: {n}"),
        }

    def initialize(self):
        # Initialize everything
        self._load_config()
        self._load_sequences()
        self._initialize_hardware()
        self._initialize_control_objects()

    def _load_config(self):
        """Load configuration from JSON file"""
        with open(self.config_path, "r") as f:
            self.config = json.load(f)

    def _load_sequences(self):
        """Load and filter sequences from CSV file"""
        df = pd.read_csv(self.sequence_path)
        self.sequences = df[df["include"] == 1]

    def _initialize_hardware(self):
        """Initialize hardware controllers based on simulation mode"""
        if self.simulation:
            self.controller = FluidControllerSimulation(self.config["microcontroller"]["serial_number"])
            self.syringe_pump = SyringePumpSimulation(
                sn=self.config["syringe_pump"]["serial_number"],
                syringe_ul=self.config["syringe_pump"]["volume_ul"],
                speed_code_limit=self.config["syringe_pump"]["speed_code_limit"],
                waste_port=3,
            )
            if (
                "temperature_controller" in self.config
                and self.config["temperature_controller"]["use_temperature_controller"]
            ):
                self.temperature_controller = TCMControllerSimulation()
        else:
            self.controller = FluidController(self.config["microcontroller"]["serial_number"])
            self.syringe_pump = SyringePump(
                sn=self.config["syringe_pump"]["serial_number"],
                syringe_ul=self.config["syringe_pump"]["volume_ul"],
                speed_code_limit=self.config["syringe_pump"]["speed_code_limit"],
                waste_port=3,
            )
            if (
                "temperature_controller" in self.config
                and self.config["temperature_controller"]["use_temperature_controller"]
            ):
                self.temperature_controller = TCMController(self.config["temperature_controller"]["serial_number"])

        self.controller.begin()
        self.controller.send_command(CMD_SET.CLEAR)

    def _initialize_control_objects(self):
        """Initialize valve system and operation objects"""
        self.selector_valve_system = SelectorValveSystem(self.controller, self.config)

        if self.config["application"] == "Open Chamber":
            self.disc_pump = DiscPump(self.controller)
            self.experiment_ops = OpenChamberOperations(
                self.config, self.syringe_pump, self.selector_valve_system, self.disc_pump
            )
        else:  # MERFISH
            self.experiment_ops = MERFISHOperations(self.config, self.syringe_pump, self.selector_valve_system)

    def run_sequences(self, section: Optional[list] = None):
        """Start running the sequences in a separate thread"""
        # If section is specified, get the subset of sequences
        sequences_to_run = self.sequences
        if section is not None:
            start_idx, end_idx = section
            sequences_to_run = self.sequences.iloc[start_idx:end_idx]

        self.worker = ExperimentWorker(self.experiment_ops, sequences_to_run, self.config, self.callbacks)
        self.thread = threading.Thread(target=self.worker.run)
        self.thread.start()

    def wait_for_completion(self):
        """Wait for the sequence thread to complete"""
        if self.thread:
            self.thread.join()

    def update_port(self, index: int):
        """Update the fluidics port for Flow Reagent sequences

        Args:
            port: New port number to use for Flow Reagent sequences with port <= 24
        """
        # Find Flow Reagent sequences with port <= 24
        mask = (self.sequences["sequence_name"] == "Flow Probe") & (self.sequences["fluidic_port"] <= 24)

        self.sequences.loc[mask, "fluidic_port"] = self.port_list[index]

    def set_rounds(self, rounds: list):
        """Rounds: a list of port indices of unique reagents to run"""
        self.port_list = rounds

    def cleanup(self):
        """Clean up hardware resources"""
        if self.syringe_pump:
            self.syringe_pump.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
