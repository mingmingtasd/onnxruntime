#-------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation.  All rights reserved.
# Licensed under the MIT License.
#--------------------------------------------------------------------------

from typing import List, Tuple, Dict
import logging
import os
import sys
from pathlib import Path
import numpy as np
from collections import deque
from onnx import onnx_pb, AttributeProto, ModelProto, TensorProto, numpy_helper, helper, external_data_helper, save_model
from shape_infer_helper import SymbolicShapeInferenceHelper

logger = logging.getLogger(__name__)


class OnnxModel:
    def __init__(self, model):
        self.model = model
        self._node_name_suffix: Dict[str, int] = {}  # key is node name prefix, value is the last suffix generated
        self.shape_infer_helper = None
        self.all_graphs = None

    def infer_runtime_shape(self, dynamic_axis_mapping={}, update=False):
        if self.shape_infer_helper is None or update:
            self.shape_infer_helper = SymbolicShapeInferenceHelper(self.model)

        try:
            if self.shape_infer_helper.infer(dynamic_axis_mapping):
                return self.shape_infer_helper
        except:
            print("failed in shape inference", sys.exc_info()[0])

        return None

    def input_name_to_nodes(self):
        input_name_to_nodes = {}
        for node in self.nodes():
            for input_name in node.input:
                if input_name not in input_name_to_nodes:
                    input_name_to_nodes[input_name] = [node]
                else:
                    input_name_to_nodes[input_name].append(node)
        return input_name_to_nodes

    def output_name_to_node(self):
        output_name_to_node = {}
        for node in self.nodes():
            for output_name in node.output:
                output_name_to_node[output_name] = node
        return output_name_to_node

    def nodes(self):
        all_nodes = []
        for graph in self.graphs():
            for node in graph.node:
                all_nodes.append(node)
        return all_nodes

    def graph(self):
        return self.model.graph

    def graphs(self):
        if self.all_graphs is not None:
            return self.all_graphs
        self.all_graphs = []
        graph_queue = [self.model.graph]
        while graph_queue:
            graph = graph_queue.pop(0)
            self.all_graphs.append(graph)
            for node in graph.node:
                for attr in node.attribute:
                    if attr.type == AttributeProto.AttributeType.GRAPH:
                        assert (isinstance(attr.g, onnx_pb.GraphProto))
                        graph_queue.append(attr.g)
                    if attr.type == AttributeProto.AttributeType.GRAPHS:
                        for g in attr.graphs:
                            assert (isinstance(g, onnx_pb.GraphProto))
                            graph_queue.append(g)
        return self.all_graphs

    def get_graphs_input_names(self):
        input_names = []
        for graph in self.graphs():
            for input in graph.input:
                input_names.append(input.name)
        return input_names

    def get_graphs_output_names(self):
        output_names = []
        for graph in self.graphs():
            for output in graph.output:
                output_names.append(output.name)
        return output_names

    def get_graph_by_node(self, node):
        for graph in self.graphs():
            if node in graph.node:
                return graph
        return None

    def get_graph_by_name(self, graph_name):
        for graph in self.graphs():
            if graph_name == graph.name:
                return graph
        return None

    def get_topological_insert_id(self, graph, outputs):
        for idx, node in enumerate(graph.node):
            for input in node.input:
                if input in outputs:
                    return idx
        return len(graph.node)

    def remove_node(self, node):
        for graph in self.graphs():
            if node in graph.node:
                graph.node.remove(node)

    def remove_nodes(self, nodes_to_remove):
        for node in nodes_to_remove:
            self.remove_node(node)

    def add_node(self, node, graph_name=None):
        if graph_name is None or graph_name == self.model.graph.name:
            self.model.graph.node.extend([node])
        else:
            graph = self.get_graph_by_name(graph_name)
            insert_idx = self.get_topological_insert_id(graph, node.output)
            graph.node.insert(insert_idx, node)

    def add_nodes(self, nodes_to_add, node_name_to_graph_name=None):
        if node_name_to_graph_name is None:
            self.model.graph.node.extend(nodes_to_add)
        else:
            for node in nodes_to_add:
                graph_name = node_name_to_graph_name[node.name]
                self.add_node(node, graph_name)

    def add_initializer(self, tensor, graph_name=None):
        if graph_name is None or graph_name == self.model.graph.name:
            self.model.graph.initializer.extend([tensor])
        else:
            graph = self.get_graph_by_name(graph_name)
            graph.initializer.extend([tensor])

    def add_input(self, input, graph_name=None):
        if graph_name is None or graph_name == self.model.graph.name:
            self.model.graph.input.extend([input])
        else:
            graph = self.get_graph_by_name(graph_name)
            graph.input.extend([input])

    @staticmethod
    def replace_node_input(node, old_input_name, new_input_name):
        assert isinstance(old_input_name, str) and isinstance(new_input_name, str)
        for j in range(len(node.input)):
            if node.input[j] == old_input_name:
                node.input[j] = new_input_name

    # This function is deprecated since we use onnxconverter-common
    def replace_input_of_all_nodes(self, old_input_name, new_input_name):
        for node in self.model.graph.node:
            OnnxModel.replace_node_input(node, old_input_name, new_input_name)

    @staticmethod
    def replace_node_output(node, old_output_name, new_output_name):
        assert isinstance(old_output_name, str) and isinstance(new_output_name, str)
        for j in range(len(node.output)):
            if node.output[j] == old_output_name:
                node.output[j] = new_output_name

    # This function is deprecated since we use onnxconverter-common
    def replace_output_of_all_nodes(self, old_output_name, new_output_name):
        for node in self.model.graph.node:
            OnnxModel.replace_node_output(node, old_output_name, new_output_name)

    def get_initializer(self, name):
        for graph in self.graphs():
            for tensor in graph.initializer:
                if tensor.name == name:
                    return tensor
        return None

    def get_nodes_by_op_type(self, op_type):
        nodes = []
        for node in self.nodes():
            if node.op_type == op_type:
                nodes.append(node)
        return nodes

    def get_children(self, node, input_name_to_nodes=None):
        if (input_name_to_nodes is None):
            input_name_to_nodes = self.input_name_to_nodes()

        children = []
        for output in node.output:
            if output in input_name_to_nodes:
                for node in input_name_to_nodes[output]:
                    children.append(node)
        return children

    def get_parents(self, node, output_name_to_node=None):
        if output_name_to_node is None:
            output_name_to_node = self.output_name_to_node()

        parents = []
        for input in node.input:
            if input in output_name_to_node:
                parents.append(output_name_to_node[input])
        return parents

    def get_parent(self, node, i, output_name_to_node=None):
        if output_name_to_node is None:
            output_name_to_node = self.output_name_to_node()

        if len(node.input) <= i:
            return None

        input = node.input[i]
        if input not in output_name_to_node:
            return None

        return output_name_to_node[input]

    def match_first_parent(self, node, parent_op_type, output_name_to_node, exclude=[]):
        '''
        Find parent node based on constraints on op_type.

        Args:
            node (str): current node name.
            parent_op_type (str): constraint of parent node op_type.
            output_name_to_node (dict): dictionary with output name as key, and node as value.
            exclude (list): list of nodes that are excluded (not allowed to match as parent).

        Returns:
            parent: The matched parent node. None if not found.
            index: The input index of matched parent node. None if not found.
        '''
        for i, input in enumerate(node.input):
            if input in output_name_to_node:
                parent = output_name_to_node[input]
                if parent.op_type == parent_op_type and parent not in exclude:
                    return parent, i
                else:
                    logger.debug(f"To find first {parent_op_type}, current {parent.op_type}")
        return None, None

    def match_parent(self,
                     node,
                     parent_op_type,
                     input_index=None,
                     output_name_to_node=None,
                     exclude=[],
                     return_indice=None):
        '''
        Find parent node based on constraints on op_type and index.
        When input_index is None, we will find the first parent node based on constraints, and return_indice will be appended the corresponding input index.

        Args:
            node (str): current node name.
            parent_op_type (str): constraint of parent node op_type.
            input_index (int or None): only check the parent given input index of current node.
            output_name_to_node (dict): dictionary with output name as key, and node as value.
            exclude (list): list of nodes that are excluded (not allowed to match as parent).
            return_indice (list): a list to append the input index when input_index is None.

        Returns:
            parent: The matched parent node.
        '''
        assert node is not None
        assert input_index is None or input_index >= 0

        if output_name_to_node is None:
            output_name_to_node = self.output_name_to_node()

        if input_index is None:
            parent, index = self.match_first_parent(node, parent_op_type, output_name_to_node, exclude)
            if return_indice is not None:
                return_indice.append(index)
            return parent

        if input_index >= len(node.input):
            logger.debug(f"input_index {input_index} >= node inputs {len(node.input)}")
            return None

        parent = self.get_parent(node, input_index, output_name_to_node)
        if parent is not None and parent.op_type == parent_op_type and parent not in exclude:
            return parent

        if parent is not None:
            logger.debug(f"Expect {parent_op_type}, Got {parent.op_type}")

        return None

    def match_parent_paths(self, node, paths, output_name_to_node):
        for i, path in enumerate(paths):
            assert isinstance(path, List) or isinstance(path, Tuple)
            return_indice = []
            matched = self.match_parent_path(node, path[0], path[1], output_name_to_node, return_indice)
            if matched:
                return i, matched, return_indice
        return -1, None, None

    def match_parent_path(self,
                          node,
                          parent_op_types,
                          parent_input_index,
                          output_name_to_node=None,
                          return_indice=None):
        '''
        Find a sequence of input edges based on constraints on parent op_type and index.
        When input_index is None, we will find the first parent node based on constraints, and return_indice will be appended the corresponding input index.

        Args:
            node (str): current node name.
            parent_op_types (str): constraint of parent node op_type of each input edge.
            parent_input_index (list): constraint of input index of each input edge. None means no constraint.
            output_name_to_node (dict): dictionary with output name as key, and node as value.
            return_indice (list): a list to append the input index when there is no constraint on input index of an edge.

        Returns:
            parents: a list of matched parent node.
        '''
        assert (len(parent_input_index) == len(parent_op_types))

        if output_name_to_node is None:
            output_name_to_node = self.output_name_to_node()

        current_node = node
        matched_parents = []
        for i, op_type in enumerate(parent_op_types):
            matched_parent = self.match_parent(current_node,
                                               op_type,
                                               parent_input_index[i],
                                               output_name_to_node,
                                               exclude=[],
                                               return_indice=return_indice)
            if matched_parent is None:
                logger.debug(f"Failed to match index={i} parent_input_index={parent_input_index[i]} op_type={op_type}",
                             stack_info=True)
                return None

            matched_parents.append(matched_parent)
            current_node = matched_parent

        return matched_parents

    def find_first_child_by_type(self, node, child_type, input_name_to_nodes=None, recursive=True):
        children = self.get_children(node, input_name_to_nodes)
        dq = deque(children)
        while len(dq) > 0:
            current_node = dq.pop()
            if current_node.op_type == child_type:
                return current_node

            if recursive:
                children = self.get_children(current_node, input_name_to_nodes)
                for child in children:
                    dq.appendleft(child)

        return None

    def find_first_parent_by_type(self, node, parent_type, output_name_to_node=None, recursive=True):
        if output_name_to_node is None:
            output_name_to_node = self.output_name_to_node()

        parents = self.get_parents(node, output_name_to_node)
        dq = deque(parents)
        while len(dq) > 0:
            current_node = dq.pop()
            if current_node.op_type == parent_type:
                return current_node

            if recursive:
                parents = self.get_parents(current_node, output_name_to_node)
                for parent in parents:
                    dq.appendleft(parent)

        return None

    def get_constant_value(self, output_name):
        for node in self.get_nodes_by_op_type('Constant'):
            if node.output[0] == output_name:
                for att in node.attribute:
                    if att.name == 'value':
                        return numpy_helper.to_array(att.t)

        # Fall back to intializer since constant folding might have been
        # applied.
        initializer = self.get_initializer(output_name)
        if initializer is not None:
            return numpy_helper.to_array(initializer)

        return None

    def get_constant_input(self, node):
        for i, input in enumerate(node.input):
            value = self.get_constant_value(input)
            if value is not None:
                return i, value

        return None, None

    def find_constant_input(self, node, expected_value, delta=0.000001):
        i, value = self.get_constant_input(node)
        if value is not None and value.size == 1 and abs(value - expected_value) < delta:
            return i

        return -1

    def is_constant_with_specified_dimension(self, output_name, dimensions, description):
        value = self.get_constant_value(output_name)
        if value is None:
            logger.debug(f"{description} {output_name} is not initializer.")
            return False

        if len(value.shape) != dimensions:
            logger.debug(f"{description} {output_name} shall have {dimensions} dimensions. Got shape {value.shape}")
            return False

        return True

    def has_constant_input(self, node, expected_value, delta=0.000001):
        return self.find_constant_input(node, expected_value, delta) >= 0

    def get_children_subgraph_nodes(self, root_node, stop_nodes, input_name_to_nodes=None):
        if input_name_to_nodes is None:
            input_name_to_nodes = self.input_name_to_nodes()

        children = input_name_to_nodes[root_node.output[0]]

        unique_nodes = []

        dq = deque(children)
        while len(dq) > 0:
            current_node = dq.pop()
            if current_node in stop_nodes:
                continue

            if current_node not in unique_nodes:
                unique_nodes.append(current_node)

                for output in current_node.output:
                    if output in input_name_to_nodes:
                        children = input_name_to_nodes[output]
                        for child in children:
                            dq.appendleft(child)

        return unique_nodes

    def tensor_shape_to_list(self, tensor_type):
        """ Convert tensor shape to list
        """
        shape_list = []
        for d in tensor_type.shape.dim:
            if (d.HasField("dim_value")):
                shape_list.append(d.dim_value)  # known dimension
            elif (d.HasField("dim_param")):
                shape_list.append(d.dim_param)  # unknown dimension with symbolic name
            else:
                shape_list.append("?")  # shall not happen
        return shape_list

    def change_input_output_float32_to_float16(self):
        """ Change graph input and output data type from FLOAT to FLOAT16
        """
        original_opset_version = self.model.opset_import[0].version
        graph = self.graph()

        new_graph_inputs = []
        for input in graph.input:
            if input.type.tensor_type.elem_type == TensorProto.FLOAT:
                new_graph_inputs.append(
                    helper.make_tensor_value_info(input.name, TensorProto.FLOAT16,
                                                  self.tensor_shape_to_list(input.type.tensor_type)))
            else:
                new_graph_inputs.append(input)

        new_graph_outputs = []
        for output in graph.output:
            if output.type.tensor_type.elem_type == TensorProto.FLOAT:
                new_graph_outputs.append(
                    helper.make_tensor_value_info(output.name, TensorProto.FLOAT16,
                                                  self.tensor_shape_to_list(output.type.tensor_type)))
            else:
                new_graph_outputs.append(output)

        graph_def = helper.make_graph(graph.node,
                                      'float16 inputs and outputs',
                                      new_graph_inputs,
                                      new_graph_outputs,
                                      initializer=graph.initializer,
                                      value_info=graph.value_info)

        self.model = helper.make_model(graph_def, producer_name='onnxruntime-tools')

        # restore opset version
        self.model.opset_import[0].version = original_opset_version

    def convert_model_float32_to_float16(self, cast_input_output=True, use_symbolic_shape_infer=True):
        """Convert a graph to FLOAT16. By default, we will keep data types of inputs and outputs.
           For decoder model with past_key_values, it is recommended to set cast_input_output=False for better performance.
        Args:
            cast_input_output (bool, optional): keep data type of inputs and outputs, and add Cast nodes to convert float32 inputs to float16, and float16 to float32 for outputs. Defaults to True.
            use_symbolic_shape_infer (bool, optional): use symbolic shape inference instead of onnx shape inference.
        """
        from packaging.version import Version
        import onnxconverter_common as oc
        if Version(oc.__version__) > Version("1.7.0"):
            model = self.model
            if use_symbolic_shape_infer:
                # Use symbolic shape inference since custom operators (like Gelu, SkipLayerNormalization etc) are not recognized by onnx shape inference.
                shape_infer_helper = SymbolicShapeInferenceHelper(model)
                model = shape_infer_helper.infer_shapes(model, auto_merge=True, guess_output_rank=False)
            self.model = oc.float16.convert_float_to_float16(model,
                                                             keep_io_types=cast_input_output,
                                                             disable_shape_infer=use_symbolic_shape_infer)
            return

        graph = self.model.graph
        initializers = graph.initializer

        for initializer in initializers:
            if initializer.data_type == 1:
                initializer.CopyFrom(
                    numpy_helper.from_array(numpy_helper.to_array(initializer).astype(np.float16), initializer.name))

        for node in graph.node:
            if node.op_type in ['Constant', 'ConstantOfShape']:
                for att in node.attribute:
                    if att.name == 'value' and att.t.data_type == 1:
                        att.CopyFrom(
                            helper.make_attribute(
                                "value", numpy_helper.from_array(numpy_helper.to_array(att.t).astype(np.float16))))
            if node.op_type == 'Cast':
                for att in node.attribute:
                    if att.name == 'to' and att.i == 1:
                        att.CopyFrom(helper.make_attribute("to", int(TensorProto.FLOAT16)))

        if not cast_input_output:
            self.change_input_output_float32_to_float16()
            return

        # Below assumes that we keep input and output data types.
        # Add Cast node to convert input from float32 to float16.
        for input_value_info in graph.input:
            if input_value_info.type.tensor_type.elem_type == TensorProto.FLOAT:
                initializer = self.get_initializer(input_value_info.name)
                if initializer is not None:  # for compatibility for old converter/exporter
                    input_value_info.type.tensor_type.elem_type = TensorProto.FLOAT16
                else:
                    cast_input = input_value_info.name
                    cast_output = input_value_info.name + '_float16'
                    self.replace_input_of_all_nodes(cast_input, cast_output)
                    cast_node = helper.make_node('Cast', inputs=[cast_input], outputs=[cast_output])
                    cast_node.attribute.extend([helper.make_attribute("to", int(TensorProto.FLOAT16))])
                    self.add_node(cast_node)

        # Add Cast node to convert output from float16 back to float32.
        for output_value_info in graph.output:
            if output_value_info.type.tensor_type.elem_type == TensorProto.FLOAT:
                cast_input = output_value_info.name + '_float16'
                cast_output = output_value_info.name
                self.replace_output_of_all_nodes(cast_output, cast_input)
                self.replace_input_of_all_nodes(cast_output, cast_input)
                cast_node = helper.make_node('Cast', inputs=[cast_input], outputs=[cast_output])
                cast_node.attribute.extend([helper.make_attribute("to", int(TensorProto.FLOAT))])
                self.add_node(cast_node)

    def create_node_name(self, op_type, name_prefix=None):
        """Create a unique node name that starts with a prefix (default is operator type).
           The name will not be duplicated with any name that generated or existed in current graphs.
        Args:
            op_type (str): operator type
            name_prefix (str, optional): prefix of node name. Defaults to None.

        Returns:
            str: node name
        """

        if name_prefix:
            prefix = name_prefix if name_prefix.endswith("_") else (name_prefix + "_")
        else:
            prefix = op_type + "_"

        suffix: int = 0
        if prefix in self._node_name_suffix:
            suffix = self._node_name_suffix[prefix] + 1
        else:
            # Check existed node name only once for a prefix as we assume create_node_name is called for every new node in fusion.
            for node in self.nodes():
                if node.name and node.name.startswith(prefix):
                    try:
                        index = int(node.name[len(prefix):])
                        suffix = max(index + 1, suffix)
                    except ValueError:
                        continue

        # Record the generated suffix so that we can avoid generating duplicated name.
        self._node_name_suffix[prefix] = suffix

        return prefix + str(suffix)

    def find_graph_input(self, input_name):
        for input in self.model.graph.input:
            if input.name == input_name:
                return input
        return None

    def find_graph_output(self, output_name):
        for output in self.model.graph.output:
            if output.name == output_name:
                return output
        return None

    def get_parent_subgraph_nodes(self, node, stop_nodes, output_name_to_node=None):
        if output_name_to_node is None:
            output_name_to_node = self.output_name_to_node()

        unique_nodes = []

        parents = self.get_parents(node, output_name_to_node)
        dq = deque(parents)
        while len(dq) > 0:
            current_node = dq.pop()
            if current_node in stop_nodes:
                continue

            if current_node not in unique_nodes:
                unique_nodes.append(current_node)

                for input in current_node.input:
                    if input in output_name_to_node:
                        dq.appendleft(output_name_to_node[input])

        return unique_nodes

    def get_graph_inputs(self, current_node, recursive=False):
        """
        Find graph inputs that linked to current node.
        """
        graph_inputs = []
        for input in current_node.input:
            if self.find_graph_input(input) and input not in graph_inputs:
                graph_inputs.append(input)

        if recursive:
            parent_nodes = self.get_parent_subgraph_nodes(current_node, [])
            for node in parent_nodes:
                for input in node.input:
                    if self.find_graph_input(input) and input not in graph_inputs:
                        graph_inputs.append(input)
        return graph_inputs

    @staticmethod
    def input_index(node_output, child_node):
        index = 0
        for input in child_node.input:
            if input == node_output:
                return index
            index += 1
        return -1

    def remove_unused_constant(self):
        input_name_to_nodes = self.input_name_to_nodes()

        #remove unused constant
        unused_nodes = []
        nodes = self.nodes()
        for node in nodes:
            if node.op_type == "Constant" and node.output[0] not in input_name_to_nodes:
                unused_nodes.append(node)

        self.remove_nodes(unused_nodes)

        if len(unused_nodes) > 0:
            logger.debug(f"Removed unused constant nodes: {len(unused_nodes)}")

    def prune_graph(self, outputs=None):
        """
        Prune graph to keep only required outputs. It removes unnecessary inputs and nodes.
        Nodes are not linked (directly or indirectly) to any required output will be removed.

        Args:
            outputs (list): a list of graph outputs to retain. If it is None, all graph outputs will be kept.
        """

        for node in self.model.graph.node:
            # Some operators with inner graph in attributes like 'body' 'else_branch' or 'then_branch'
            if node.op_type in ['Loop', 'Scan', 'If']:
                # TODO: handle inner graph
                logger.debug(f"Skip prune_graph since graph has operator: {node.op_type}")
                return

        if outputs is None:
            outputs = [output.name for output in self.model.graph.output]

        output_name_to_node = self.output_name_to_node()
        all_nodes = []
        for output in outputs:
            if output in output_name_to_node:
                last_node = output_name_to_node[output]
                if last_node in all_nodes:
                    continue
                nodes = self.get_parent_subgraph_nodes(last_node, [])
                all_nodes.append(last_node)
                all_nodes.extend(nodes)

        nodes_to_remove = []
        for node in self.model.graph.node:
            if node not in all_nodes:
                nodes_to_remove.append(node)

        self.remove_nodes(nodes_to_remove)

        # remove outputs not in list
        output_to_remove = []
        for output in self.model.graph.output:
            if output.name not in outputs:
                output_to_remove.append(output)
        for output in output_to_remove:
            self.model.graph.output.remove(output)

        # remove inputs not used by any node.
        input_name_to_nodes = self.input_name_to_nodes()
        input_to_remove = []
        for input in self.model.graph.input:
            if input.name not in input_name_to_nodes:
                input_to_remove.append(input)
        for input in input_to_remove:
            self.model.graph.input.remove(input)

        logger.info("Graph pruned: {} inputs, {} outputs and {} nodes are removed".format(
            len(input_to_remove), len(output_to_remove), len(nodes_to_remove)))

        self.update_graph()

    def update_graph(self, verbose=False):
        graph = self.model.graph

        remaining_input_names = []
        for node in graph.node:
            if node.op_type in ['Loop', 'Scan', 'If']:
                # TODO: handle inner graph
                logger.debug(f"Skip update_graph since graph has operator: {node.op_type}")
                return
            if node.op_type != "Constant":
                for input_name in node.input:
                    if input_name not in remaining_input_names:
                        remaining_input_names.append(input_name)
        if verbose:
            logger.debug(f"remaining input names: {remaining_input_names}")

        # remove graph input that is not used
        inputs_to_remove = []
        for input in graph.input:
            if input.name not in remaining_input_names:
                inputs_to_remove.append(input)
        for input in inputs_to_remove:
            graph.input.remove(input)

        names_to_remove = [input.name for input in inputs_to_remove]
        logger.debug(f"remove {len(inputs_to_remove)} unused inputs: {names_to_remove}")

        # remove weights that are not used
        weights_to_remove = []
        weights_to_keep = []
        for initializer in graph.initializer:
            if initializer.name not in remaining_input_names and not self.find_graph_output(initializer.name):
                weights_to_remove.append(initializer)
            else:
                weights_to_keep.append(initializer.name)
        for initializer in weights_to_remove:
            graph.initializer.remove(initializer)

        names_to_remove = [initializer.name for initializer in weights_to_remove]
        logger.debug(f"remove {len(weights_to_remove)} unused initializers: {names_to_remove}")
        if verbose:
            logger.debug(f"remaining initializers:{weights_to_keep}")

        self.remove_unused_constant()

    def is_safe_to_fuse_nodes(self, nodes_to_remove, keep_outputs, input_name_to_nodes, output_name_to_node):
        for node_to_remove in nodes_to_remove:
            for output_to_remove in node_to_remove.output:
                if output_to_remove in keep_outputs:
                    continue

                if output_to_remove in input_name_to_nodes:
                    for impacted_node in input_name_to_nodes[output_to_remove]:
                        if impacted_node not in nodes_to_remove:
                            logger.debug(
                                f"it is not safe to remove nodes since output {output_to_remove} is used by {impacted_node}"
                            )
                            return False
        return True

    @staticmethod
    def graph_topological_sort(graph):
        deps_count = [0] * len(graph.node)  # dependency count of each node
        deps_to_nodes = {}  # input to node indice
        sorted_nodes = []  # initialize sorted_nodes
        for node_idx, node in enumerate(graph.node):
            # CANNOT use len(node.input) directly because input can be optional
            deps_count[node_idx] = sum(1 for _ in node.input if _)
            if deps_count[node_idx] == 0:  # Constant doesn't depend on any inputs
                sorted_nodes.append(graph.node[node_idx])
                continue

            for input_name in node.input:
                if input_name not in deps_to_nodes:
                    deps_to_nodes[input_name] = [node_idx]
                else:
                    deps_to_nodes[input_name].append(node_idx)

        # Note: this logic only applies to top level graph since a sub graph could use intializer from parent graph
        initializer_names = [init.name for init in graph.initializer]
        graph_input_names = [input.name for input in graph.input]
        input_names = initializer_names + graph_input_names
        input_names.sort()
        prev_input_name = None
        for input_name in input_names:
            if prev_input_name == input_name:
                continue

            prev_input_name = input_name
            if input_name in deps_to_nodes:
                for node_idx in deps_to_nodes[input_name]:
                    deps_count[node_idx] = deps_count[node_idx] - 1
                    if deps_count[node_idx] == 0:
                        sorted_nodes.append(graph.node[node_idx])

        start = 0
        end = len(sorted_nodes)

        while start < end:
            for output in sorted_nodes[start].output:
                if output in deps_to_nodes:
                    for node_idx in deps_to_nodes[output]:
                        deps_count[node_idx] = deps_count[node_idx] - 1
                        if deps_count[node_idx] == 0:
                            sorted_nodes.append(graph.node[node_idx])
                            end = end + 1
            start = start + 1

        assert (end == len(graph.node)), "Graph is not a DAG"
        graph.ClearField('node')
        graph.node.extend(sorted_nodes)

    def topological_sort(self):
        #TODO: support graph_topological_sort() in subgraphs
        #for graph in self.graphs():
        #    self.graph_topological_sort(graph)
        OnnxModel.graph_topological_sort(self.model.graph)

    def save_model_to_file(self, output_path, use_external_data_format=False):
        logger.info(f"Sort graphs in topological order")
        self.topological_sort()

        logger.info(f"Output model to {output_path}")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        if output_path.endswith(".json"):  # Output text for testing small model.
            assert isinstance(self.model, ModelProto)
            with open(output_path, "w") as out:
                out.write(str(self.model))
        else:
            # Save model to external data, which is needed for model size > 2GB
            if use_external_data_format:
                data_file = str(Path(output_path).name + ".data")
                if os.path.isfile(data_file):
                    os.remove(data_file)
                external_data_helper.convert_model_to_external_data(self.model,
                                                                    all_tensors_to_one_file=True,
                                                                    location=data_file)
            save_model(self.model, output_path)

    def get_graph_inputs_excluding_initializers(self):
        """
        Returns real graph inputs (excluding initializers from older onnx model).
        """
        graph_inputs = []
        for input in self.model.graph.input:
            if self.get_initializer(input.name) is None:
                graph_inputs.append(input)
        return graph_inputs

    def get_opset_version(self):
        """Get opset version of onnx domain

        Raises:
            RuntimeError: ONNX model has no opset for default domain.

        Returns:
            int: opset version of onnx domain.
        """
        for opset in self.model.opset_import:
            if opset.domain in ["", "ai.onnx"]:
                return opset.version
        raise RuntimeError("ONNX model has no opset for default domain")
