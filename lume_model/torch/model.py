import os
import logging
from typing import Union

import torch
from pydantic import validator
from botorch.models.transforms.input import ReversibleInputTransform

from lume_model.base import LUMEBaseModel
from lume_model.variables import (
    InputVariable,
    OutputVariable,
    ScalarInputVariable,
    ScalarOutputVariable,
    ImageOutputVariable,
)

logger = logging.getLogger(__name__)


class TorchModel(LUMEBaseModel):
    """LUME-model class for torch models.

    By default, the models are assumed to be fixed, so all gradient computation is deactivated and the model and
    transformers are put in evaluation mode.

    Attributes:
        model: The torch base model.
        input_variables: List defining the input variables and their order.
        output_variables: List defining the output variables and their order.
        input_transformers: List of transformer objects to apply to input before passing to model.
        output_transformers: List of transformer objects to apply to output of model.
        output_format: Determines format of outputs.
        fixed_model: If true, the model and transformers are put in evaluation mode and all gradient
          computation is deactivated.
        device: Device on which the model will be evaluated. Defaults to "cpu".
    """
    model: torch.nn.Module
    input_transformers: list[ReversibleInputTransform] = []
    output_transformers: list[ReversibleInputTransform] = []
    output_format: dict[str, str] = {"type": "tensor"}
    device: Union[torch.device, str] = "cpu"
    fixed_model: bool = True

    def __init__(
            self,
            config: Union[dict, str] = None,
            **kwargs,
    ):
        """Initializes TorchModel.

        Args:
            config: Model configuration as dictionary, YAML or JSON formatted string or file path. This overrides
              all other arguments.
            **kwargs: See class attributes.
        """
        super().__init__(config, **kwargs)

        # set precision
        self.model.double()
        for t in self.input_transformers + self.output_transformers:
            if isinstance(t, torch.nn.Module):
                t.double()

        # fixed model: set full model in eval mode and deactivate all gradients
        if self.fixed_model:
            self.model.eval().requires_grad_(False)
            for t in self.input_transformers + self.output_transformers:
                if isinstance(t, torch.nn.Module):
                    t.eval().requires_grad_(False)

        # ensure consistent device
        self.to(self.device)

    @validator("model", pre=True)
    def validate_torch_model(cls, v):
        if isinstance(v, (str, os.PathLike)):
            if os.path.exists(v):
                v = torch.load(v)
        return v

    @validator("input_transformers", "output_transformers", pre=True)
    def validate_botorch_transformers(cls, v):
        if not isinstance(v, list):
            raise ValueError("Transformers must be passed as list.")
        else:
            loaded_transformers = []
            for t in v:
                if isinstance(t, (str, os.PathLike)):
                    if os.path.exists(t):
                        t = torch.load(t)
                loaded_transformers.append(t)
            v = loaded_transformers
        return v

    def evaluate(
            self,
            input_dict: dict[str, Union[InputVariable, float, torch.Tensor]],
    ) -> dict[str, Union[OutputVariable, float, torch.Tensor]]:
        """Evaluates model on the given input dictionary.

        Args:
            input_dict: Input dictionary on which to evaluate the model.

        Returns:
            Dictionary of output variable names to values.
        """
        formatted_inputs = self._format_inputs(input_dict)
        input_tensor = self._arrange_inputs(formatted_inputs)
        input_tensor = self._transform_inputs(input_tensor)
        output_tensor = self.model(input_tensor)
        output_tensor = self._transform_outputs(output_tensor)
        parsed_outputs = self._parse_outputs(output_tensor)
        output_dict = self._prepare_outputs(parsed_outputs)
        return output_dict

    def random_input(self, n_samples: int = 1) -> dict[str, torch.Tensor]:
        """Generates random input(s) for the model.

        Args:
            n_samples: Number of random samples to generate.

        Returns:
            Dictionary of input variable names to tensors.
        """
        input_dict = {}
        for var in self.input_variables:
            if isinstance(var, ScalarInputVariable):
                input_dict[var.name] = var.value_range[0] + torch.rand(size=(n_samples,)) * (
                            var.value_range[1] - var.value_range[0])
            else:
                torch.tensor(var.default, dtype=torch.double).repeat((n_samples, 1))
        return input_dict

    def random_evaluate(self, n_samples: int = 1) -> dict[str, Union[OutputVariable, float, torch.Tensor]]:
        """Returns random evaluation(s) of the model.

        Args:
            n_samples: Number of random samples to evaluate.

        Returns:
            Dictionary of variable names to outputs.
        """
        random_input = self.random_input(n_samples)
        return self.evaluate(random_input)

    def to(self, device: Union[torch.device, str]):
        """Updates the device for the model, transformers and default values.

        Args:
            device: Device on which the model will be evaluated.
        """
        self.model.to(device)
        for t in self.input_transformers + self.output_transformers:
            if isinstance(t, torch.nn.Module):
                t.to(device)
        self.device = device

    def insert_input_transformer(self, new_transformer: ReversibleInputTransform, loc: int):
        """Inserts an additional input transformer at the given location.

        Args:
            new_transformer: New transformer to add.
            loc: Location where the new transformer shall be added to the transformer list.
        """
        self.input_transformers = (self.input_transformers[:loc] + [new_transformer] +
                                   self.input_transformers[loc:])

    def insert_output_transformer(self, new_transformer: ReversibleInputTransform, loc: int):
        """Inserts an additional output transformer at the given location.

        Args:
            new_transformer: New transformer to add.
            loc: Location where the new transformer shall be added to the transformer list.
        """
        self.output_transformers = (self.output_transformers[:loc] + [new_transformer] +
                                    self.output_transformers[loc:])

    def _format_inputs(
            self,
            input_dict: dict[str, Union[InputVariable, float, torch.Tensor]],
    ) -> dict[str, torch.Tensor]:
        """Formats values of the input dictionary as tensors.

        Args:
            input_dict: Dictionary of input variable names to values.

        Returns:
            Dictionary of input variable names to tensors.
        """
        # NOTE: The input variable is only updated if a singular value is given (ambiguous otherwise)
        formatted_inputs = {}
        for var_name, var in input_dict.items():
            if isinstance(var, InputVariable):
                formatted_inputs[var_name] = torch.tensor(var.value, dtype=torch.double, device=self.device)
                self.input_variables[self.input_names.index(var_name)].value = var.value
            elif isinstance(var, float):
                formatted_inputs[var_name] = torch.tensor(var, dtype=torch.double, device=self.device)
                self.input_variables[self.input_names.index(var_name)].value = var
            elif isinstance(var, torch.Tensor):
                var = var.double().squeeze().to(self.device)
                formatted_inputs[var_name] = var
                if var.dim() == 0:
                    self.input_variables[self.input_names.index(var_name)].value = var.item()
            else:
                TypeError(
                    f"Unknown type {type(var)} passed to evaluate."
                    f"Should be one of InputVariable, float or torch.Tensor."
                )
        return formatted_inputs

    def _arrange_inputs(self, formatted_inputs: dict[str, torch.Tensor]) -> torch.Tensor:
        """Enforces order of input variables.

        Enforces the order of the input variables to be passed to the transformers and model and updates the
        returned tensor with default values for any inputs that are missing.

        Args:
            formatted_inputs: Dictionary of input variable names to tensors.

        Returns:
            Ordered input tensor to be passed to the transformers.
        """
        default_tensor = torch.tensor(
            [var.default for var in self.input_variables], dtype=torch.double, device=self.device
        )

        # determine input shape
        input_shapes = [formatted_inputs[k].shape for k in formatted_inputs.keys()]
        if not all(ele == input_shapes[0] for ele in input_shapes):
            raise ValueError("Inputs have inconsistent shapes.")

        input_tensor = torch.tile(default_tensor, dims=(*input_shapes[0], 1))
        for key, value in formatted_inputs.items():
            input_tensor[..., self.input_names.index(key)] = value

        if input_tensor.shape[-1] != len(self.input_names):
            raise ValueError(
                f"""
                Last dimension of input tensor doesn't match the expected number of inputs\n
                received: {default_tensor.shape}, expected {len(self.input_names)} as the last dimension
                """
            )
        return input_tensor

    def _transform_inputs(self, input_tensor: torch.Tensor) -> torch.Tensor:
        """Applies transformations to the inputs.

        Args:
            input_tensor: Ordered input tensor to be passed to the transformers.

        Returns:
            Tensor of transformed inputs to be passed to the model.
        """
        for transformer in self.input_transformers:
            input_tensor = transformer.transform(input_tensor)
        return input_tensor

    def _transform_outputs(self, output_tensor: torch.Tensor) -> torch.Tensor:
        """(Un-)Transforms the model output tensor.

        Args:
            output_tensor: Output tensor from the model.

        Returns:
            (Un-)Transformed output tensor.
        """
        for transformer in self.output_transformers:
            output_tensor = transformer.untransform(output_tensor)
        return output_tensor

    def _parse_outputs(self, output_tensor: torch.Tensor) -> dict[str, torch.Tensor]:
        """Constructs dictionary from model output tensor.

        Args:
            output_tensor: (Un-)transformed output tensor from the model.

        Returns:
            Dictionary of output variable names to (un-)transformed tensors.
        """
        parsed_outputs = {}
        if output_tensor.dim() in [0, 1]:
            output_tensor = output_tensor.unsqueeze(0)
        if len(self.output_names) == 1:
            parsed_outputs[self.output_names[0]] = output_tensor.squeeze()
        else:
            for idx, output_name in enumerate(self.output_names):
                parsed_outputs[output_name] = output_tensor[..., idx].squeeze()
        return parsed_outputs

    def _prepare_outputs(
            self,
            parsed_outputs: dict[str, torch.Tensor],
    ) -> dict[str, Union[OutputVariable, torch.Tensor]]:
        """Updates and returns outputs according to output_format.

        Updates the output variables within the model to reflect the new values.

        Args:
            parsed_outputs: Dictionary of output variable names to transformed tensors.

        Returns:
            Dictionary of output variable names to values depending on output_format.
        """
        for var in self.output_variables:
            if parsed_outputs[var.name].dim() == 0:
                idx = self.output_names.index(var.name)
                if isinstance(var, ScalarOutputVariable):
                    self.output_variables[idx].value = parsed_outputs[var.name].item()
                elif isinstance(var, ImageOutputVariable):
                    # OutputVariables should be numpy arrays
                    self.output_variables[idx].value = (parsed_outputs[var.name].reshape(var.shape).numpy())
                    self._update_image_limits(var, parsed_outputs)

        if self.output_format.get("type") == "tensor":
            return parsed_outputs
        elif self.output_format.get("type") == "variable":
            return {var.name: var for var in self.output_variables}
        else:
            return {var.name: var.value for var in self.output_variables}

    def _update_image_limits(
            self,
            variable: OutputVariable, predicted_output: dict[str, torch.Tensor],
    ):
        output_idx = self.output_names.index(variable.name)
        if self.output_variables[output_idx].x_min_variable:
            self.output_variables[output_idx].x_min = predicted_output[
                self.output_variables[output_idx].x_min_variable
            ].item()

        if self.output_variables[output_idx].x_max_variable:
            self.output_variables[output_idx].x_max = predicted_output[
                self.output_variables[output_idx].x_max_variable
            ].item()

        if self.output_variables[output_idx].y_min_variable:
            self.output_variables[output_idx].y_min = predicted_output[
                self.output_variables[output_idx].y_min_variable
            ].item()

        if self.output_variables[output_idx].y_max_variable:
            self.output_variables[output_idx].y_max = predicted_output[
                self.output_variables[output_idx].y_max_variable
            ].item()
