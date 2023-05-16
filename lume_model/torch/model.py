import logging
from typing import Dict, List, Optional, Tuple, Union

import torch
from botorch.models.transforms.input import ReversibleInputTransform

from lume_model.models import BaseModel
from lume_model.variables import InputVariable, OutputVariable

logger = logging.getLogger(__name__)


class PyTorchModel(BaseModel):
    """The PyTorchModel class is used for the loading and evaluation of online models.
    It is  designed to implement the general behaviors expected for models used with
    the pytorch lume-model tool kit."""

    def __init__(
        self,
        model_file: str,
        input_variables: Dict[str, InputVariable],
        output_variables: Dict[str, OutputVariable],
        input_transformers: Optional[List[ReversibleInputTransform]] = [],
        output_transformers: Optional[List[ReversibleInputTransform]] = [],
        output_format: Optional[Dict[str, str]] = {"type": "tensor"},
        feature_order: Optional[list] = None,
        output_order: Optional[list] = None,
        device: Optional[Union[torch.device, str]] = "cpu",
    ) -> None:
        """Initializes the model, stores inputs/outputs and determines the format
        in which the model results will be output.

        Args:
            model_file (str): Path to model file generated with torch.save()
            input_variables (Dict[str, InputVariable]): list of model input variables
            output_variables (Dict[str, OutputVariable]): list of model output variables
            input_transformers: (List[ReversibleInputTransform]): list of transformer
                objects to apply to input before passing to model
            output_transformers: (List[ReversibleInputTransform]): list of transformer
                objects to apply to output of model
            output_format (Optional[dict]): Wrapper for interpreting outputs. This now handles
                raw or softmax values, but should be expanded to accomodate misc
                functions. Now, dictionary should look like:
                    {"type": Literal["raw", "string", "tensor", "variable"]}
            feature_order: List[str]: list containing the names of features in the
                order in which they are passed to the model
            output_order: List[str]: list containing the names of outputs in the
                order the model produces them
            device (Optional[Union[torch.device, str]]): Device on which the
              model will be evaluated. Defaults to "cpu".

        TODO: make list of Transformer objects into botorch ChainedInputTransform?

        """
        super(BaseModel, self).__init__()

        # Save init
        self.device = device
        self.input_variables = input_variables
        self.default_values = torch.tensor(
            [var.default for var in input_variables.values()],
            dtype=torch.double,
            requires_grad=True,
            device=self.device
        )
        self.output_variables = output_variables
        self._model_file = model_file
        self._output_format = output_format

        # make sure all of the transformers are in eval mode and on device
        self._input_transformers = input_transformers
        self._output_transformers = output_transformers
        for transformer in self._input_transformers + self._output_transformers:
            transformer.eval()
            transformer.to(self.device)

        self._model = torch.load(model_file).double()
        self._model.eval()
        self._model.to(self.device)

        self._feature_order = feature_order
        self._output_order = output_order

    @property
    def features(self):
        if self._feature_order is not None:
            return self._feature_order
        else:
            # if there's no specified order, we make the assumption
            # that the variables were passed in the desired order
            # in the configuration file
            return list(self.input_variables.keys())

    @property
    def outputs(self):
        if self._output_order is not None:
            return self._output_order
        else:
            # if there's no order specified, we assume it's the same as the
            # order passed in the variables.yml file
            return list(self.output_variables.keys())

    @property
    def input_transformers(self):
        return self._input_transformers

    @property
    def output_transformers(self):
        return self._output_transformers

    @input_transformers.setter
    def input_transformers(self, new_transformer: Tuple[ReversibleInputTransform, int]):
        transformer, loc = new_transformer
        self._input_transformers.insert(loc, transformer)

    @output_transformers.setter
    def output_transformers(
        self, new_transformer: Tuple[ReversibleInputTransform, int]
    ):
        transformer, loc = new_transformer
        self._output_transformers.insert(loc, transformer)

    def evaluate(
        self,
        input_variables: Dict[str, Union[InputVariable, float, torch.Tensor]],
    ) -> Dict[str, Union[torch.Tensor, OutputVariable, float]]:
        """Evaluate model using new input variables.

        Args:
            input_variables (Dict[str, InputVariable]): List of updated input
                variables

        Returns:
            Dict[str, torch.Tensor]: Dictionary mapping var names to outputs

        """
        # all PyTorch models will follow the same process, the inputs
        # are formatted, then converted to model features. Then they
        # are passed through the model, and transformed again on the
        # other side. The final dictionary is then converted into a
        # useful form
        input_vals = self._prepare_inputs(input_variables)
        input_vals = self._arrange_inputs(input_vals)
        features = self._transform_inputs(input_vals)
        raw_output = self._model(features)
        transformed_output = self._transform_outputs(raw_output)
        output = self._parse_outputs(transformed_output)
        output = self._prepare_outputs(output)

        return output

    def _prepare_inputs(
        self, input_variables: Dict[str, Union[InputVariable, float, torch.Tensor]]
    ) -> Dict[str, torch.Tensor]:
        """
        Prepares the input variables dictionary as a format appropriate
        to be passed to the transformers and updates the stored InputVariables
        with new values

        Args:
            input_variables (dict): Dictionary of input variable names to
                variables in any format (InputVariable or raw values)

        Returns:
            dict (Dict[str, torch.Tensor]): dictionary of input variable
                values to be passed to the transformers
        """
        # NOTE we only update the input variable if we receive a singular
        # value, otherwise we don't know which value to assign so we just
        # leave it
        model_vals = {}
        for var_name, var in input_variables.items():
            if isinstance(var, InputVariable):
                model_vals[var_name] = torch.tensor(
                    var.value, dtype=torch.double, requires_grad=True,
                    device=self.device
                )
                self.input_variables[var_name].value = var.value
            elif isinstance(var, float):
                model_vals[var_name] = torch.tensor(
                    var, dtype=torch.double, requires_grad=True,
                    device=self.device
                )
                self.input_variables[var_name].value = var
            elif isinstance(var, torch.Tensor):
                var = var.double().squeeze().to(self.device)
                if not var.requires_grad:
                    var.requires_grad = True
                model_vals[var_name] = var
                if var.dim() == 0:
                    self.input_variables[var_name].value = var.item()
            else:
                TypeError(
                    f"Unknown type {type(var)} passed to evaluate. Should be one of InputVariable, float or torch.Tensor"
                )
        return model_vals

    def _arrange_inputs(self, input_variables: Dict[str, torch.Tensor]) -> torch.Tensor:
        """
        Enforces the order of the input variables to be passed to the transformers
        and models and updates the model with default values for any features that
        are missing, maintaining the shape of the incoming features.

        Args:
            input_variables (dict): Dictionary of input variable names to raw
                values of inputs

        Returns:
            torch.Tensor: ordered tensor of input variables to be passed to the
                transformers

        """
        incoming_shape = list(input_variables.items())[0][1].unsqueeze(-1).shape
        default_tensor = torch.tile(self.default_values, incoming_shape)

        for key, value in input_variables.items():
            feature_idx = self.features.index(key)
            default_tensor[..., feature_idx] = value

        if default_tensor.shape[-1] != len(self.features):
            raise ValueError(
                f"""
                Last dimension of input tensor doesn't match the expected number of features\n
                received: {default_tensor.shape}, expected {len(self.features)} as the last dimension
                """
            )
        else:
            return default_tensor

    def _transform_inputs(self, input_values: torch.Tensor) -> torch.Tensor:
        """
        Applies transformations to the inputs

        Args:
            input_values (torch.Tensor): tensor of input variables to be passed
                to the transformers

        Returns:
            torch.Tensor: tensor of transformed input variables to be passed
                to the model
        """
        for transformer in self._input_transformers:
            input_values = transformer(input_values)
        return input_values

    def _transform_outputs(self, model_output: torch.Tensor) -> torch.Tensor:
        """
        Untransforms the model outputs to real units

        Args:
            model_output (torch.Tensor): tensor of outputs from the model

        Returns:
            Dict[str, torch.Tensor]: dictionary of variable name to tensor
                of untransformed output variables
        """
        # NOTE do we need to sort these to reverse them?
        for transformer in self._output_transformers:
            model_output = transformer.untransform(model_output)
        return model_output

    def _parse_outputs(self, model_output: torch.Tensor) -> Dict[str, torch.Tensor]:
        """
        Constructs dictionary from model outputs

        Args:
            model_output (torch.Tensor): transformed output from NN model

        Returns:
            Dict[str, torch.Tensor]: dictionary of output variable name to output
                value
        """
        # NOTE if we have shape [50,3,1] coming out of the model, our output
        # dictionary should have shape [50,3]
        output = {}
        if model_output.dim() == 1 or model_output.dim() == 0:
            model_output = model_output.unsqueeze(0)
        for idx, output_name in enumerate(self.outputs):
            output[output_name] = model_output[..., idx].squeeze()
        return output

    def _prepare_outputs(
        self, predicted_output: Dict[str, torch.Tensor]
    ) -> Dict[str, Union[OutputVariable, torch.Tensor]]:
        """
        Updates the output variables within the model to reflect the new values
        if we only have a singular data point.
        Args:
            predicted_output (Dict[str, torch.Tensor]): Dictionary of output
                variable name to value

        Returns:
            Dict[str, Union[OutputVariable,torch.Tensor]]: Dictionary of output
                variable name to output tensor or OutputVariable depending
                on model's _ouptut_format
        """
        for variable in self.output_variables.values():
            if predicted_output[variable.name].dim() == 0:
                if variable.variable_type == "scalar":
                    self.output_variables[variable.name].value = predicted_output[
                        variable.name
                    ].item()
                elif variable.variable_type == "image":
                    # OutputVariables should be numpy arrays so we need to convert
                    # the tensor to a numpy array
                    self.output_variables[variable.name].value = (
                        predicted_output[variable.name].reshape(variable.shape).numpy()
                    )
                    self._update_image_limits(variable, predicted_output)

        if self._output_format.get("type") == "tensor":
            return predicted_output
        elif self._output_format.get("type") == "variable":
            return self.output_variables
        else:
            return {key: var.value for key, var in self.output_variables.items()}

    def _update_image_limits(
        self, variable: OutputVariable, predicted_output: Dict[str, torch.Tensor]
    ):
        # update limits
        if self.output_variables[variable.name].x_min_variable:
            self.output_variables[variable.name].x_min = predicted_output[
                self.output_variables[variable.name].x_min_variable
            ].item()

        if self.output_variables[variable.name].x_max_variable:
            self.output_variables[variable.name].x_max = predicted_output[
                self.output_variables[variable.name].x_max_variable
            ].item()

        if self.output_variables[variable.name].y_min_variable:
            self.output_variables[variable.name].y_min = predicted_output[
                self.output_variables[variable.name].y_min_variable
            ].item()

        if self.output_variables[variable.name].y_max_variable:
            self.output_variables[variable.name].y_max = predicted_output[
                self.output_variables[variable.name].y_max_variable
            ].item()

    def to(self, device: Union[torch.device, str]):
        """Updates the device for the model, transformers and default values.

        Args:
            device: Device on which the model will be evaluated.
        """
        self._model.to(device)
        for transformer in self._input_transformers + self._output_transformers:
            transformer.to(device)
        self.default_values = self.default_values.to(device)
        self.device = device
