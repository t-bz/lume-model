import json
from copy import deepcopy
from pathlib import Path
from pprint import pprint

import pytest
import torch
from botorch.models.transforms.input import AffineInputTransform

from lume_model.pytorch import PyTorchModel
from lume_model.utils import model_from_yaml, variables_from_yaml
from lume_model.variables import ScalarInputVariable, ScalarOutputVariable

"""
Things to Test:
---------------
- [x] we can load a PyTorch model from a yaml file
    - [x] returning the model class and keywords
    - [x] returning the model instance
- [x] we can create a PyTorch model from objects
- [x] pytorch model can be run using dictionary of:
    - [x] tensors
    - [x] InputVariables
    - [x] floats
- [x] pytorch model evaluate() can return dictionary of either tensors
    or OutputVariables
- [x] pytorch model can be run with transformers or without
- [x] if we pass in a dictionary that's missing a value, we log
    an error and use the default value for the input
- [x] passing different input dictionaries through gives us different
    output dictionaries
- [x] differentiability through the model (required for Xopt)
- [ ] output transformations are applied in the correct order when we
    have multiple transformations
"""
tests_dir = str(Path(__file__).parent.parent)

with open(f"{tests_dir}/test_files/california_regression/model_info.json", "r") as f:
    model_info = json.load(f)

with open(
    f"{tests_dir}/test_files/california_regression/california_variables.yml", "r"
) as f:
    input_variables, output_variables = variables_from_yaml(f)

with open(f"{tests_dir}/test_files/california_regression/normalization.json", "r") as f:
    normalizations = json.load(f)

input_transformer = AffineInputTransform(
    len(normalizations["x_mean"]),
    coefficient=torch.tensor(normalizations["x_scale"]),
    offset=torch.tensor(normalizations["x_mean"]),
)
output_transformer = AffineInputTransform(
    len(normalizations["y_mean"]),
    coefficient=torch.tensor(normalizations["y_scale"]),
    offset=torch.tensor(normalizations["y_mean"]),
)
model_kwargs = {
    "model_file": f"{tests_dir}/test_files/california_regression/california_regression.pt",
    "input_variables": input_variables,
    "output_variables": output_variables,
    "input_transformers": [input_transformer],
    "output_transformers": [output_transformer],
    "feature_order": model_info["model_in_list"],
    "output_order": model_info["model_out_list"],
    "output_format": {"type": "tensor"},
}
test_x = torch.load(f"{tests_dir}/test_files/california_regression/X_test_raw.pt")
# for speed/memory in tests we set requires grad to false and only activate it
# when testing for differentiability
test_x.requires_grad = False
test_x_dict = {
    key: test_x[0][idx] for idx, key in enumerate(model_info["model_in_list"])
}


def assert_variables_updated(
    input_value: float,
    output_value: float,
    model: PyTorchModel,
    input_name: str,
    output_name: str,
):
    """helper function to verify that model input_variables and output_variables
    have been updated correctly with float values (NOT tensors)"""
    assert isinstance(model.input_variables[input_name].value, float)
    assert model.input_variables[input_name].value == pytest.approx(input_value)
    assert isinstance(model.output_variables[output_name].value, float)
    assert model.output_variables[output_name].value == pytest.approx(output_value)


def test_model_from_yaml():
    with open(
        f"{tests_dir}/test_files/california_regression/california_variables.yml", "r"
    ) as f:
        test_model, test_model_kwargs = model_from_yaml(f, load_model=False)

    assert test_model == PyTorchModel
    for key in list(model_kwargs.keys()):
        # we don't define anything about the transformers in the yml file so we
        # don't expect there to be anything in the model_kwargs about them
        if key not in ["input_transformers", "output_transformers"]:
            assert key in list(test_model_kwargs.keys())


def test_model_from_yaml_load_model():
    with open(
        f"{tests_dir}/test_files/california_regression/california_variables.yml", "r"
    ) as f:
        test_model = model_from_yaml(f, load_model=True)

    assert isinstance(test_model, PyTorchModel)
    assert test_model.input_variables == input_variables
    assert test_model.output_variables == output_variables
    assert test_model.features == model_kwargs["feature_order"]
    assert test_model.outputs == model_kwargs["output_order"]
    assert test_model._input_transformers == []
    assert test_model._output_transformers == []

    # now we want to test whether we can add the transformers afterwards
    test_model.input_transformers = (input_transformer, 0)
    test_model.output_transformers = (output_transformer, 0)
    assert test_model.input_transformers == [input_transformer]
    assert test_model.output_transformers == [output_transformer]


def test_model_from_objects():
    cal_model = PyTorchModel(**model_kwargs)

    assert cal_model._feature_order == model_info["model_in_list"]
    assert cal_model._output_order == model_info["model_out_list"]
    assert isinstance(cal_model, PyTorchModel)
    assert cal_model.input_variables == input_variables
    assert cal_model.output_variables == output_variables
    assert cal_model.features == model_kwargs["feature_order"]
    assert cal_model.outputs == model_kwargs["output_order"]
    assert cal_model.input_transformers == [input_transformer]
    assert cal_model.output_transformers == [output_transformer]


def test_california_housing_model_variable():
    args = deepcopy(model_kwargs)
    args["output_format"] = {"type": "variable"}
    cal_model = PyTorchModel(**args)

    input_variables_dict = deepcopy(cal_model.input_variables)
    for key, var in input_variables_dict.items():
        var.value = test_x_dict[key].item()

    results = cal_model.evaluate(input_variables_dict)

    assert isinstance(results["MedHouseVal"], ScalarOutputVariable)
    assert results["MedHouseVal"].value == pytest.approx(4.063651)
    assert_variables_updated(
        test_x_dict["HouseAge"].item(), 4.063651, cal_model, "HouseAge", "MedHouseVal"
    )


def test_california_housing_model_tensor():
    cal_model = PyTorchModel(**model_kwargs)

    results = cal_model.evaluate(test_x_dict)

    assert torch.isclose(
        results["MedHouseVal"], torch.tensor(4.063651, dtype=torch.double)
    )
    assert isinstance(results["MedHouseVal"], torch.Tensor)
    assert_variables_updated(
        test_x_dict["HouseAge"].item(), 4.063651, cal_model, "HouseAge", "MedHouseVal"
    )


def test_california_housing_model_float():
    args = deepcopy(model_kwargs)
    args["output_format"] = {"type": "raw"}
    cal_model = PyTorchModel(**args)

    float_dict = {key: value.item() for key, value in test_x_dict.items()}

    results = cal_model.evaluate(float_dict)

    assert results["MedHouseVal"] == pytest.approx(4.063651)
    assert isinstance(results["MedHouseVal"], float)
    assert_variables_updated(
        test_x_dict["HouseAge"].item(), 4.063651, cal_model, "HouseAge", "MedHouseVal"
    )


@pytest.mark.parametrize(
    "test_input,expected",
    [
        (
            {
                key: test_x[0][idx]
                for idx, key in enumerate(model_info["model_in_list"])
            },
            torch.tensor(4.063651, dtype=torch.double),
        ),
        (
            {
                key: test_x[1][idx]
                for idx, key in enumerate(model_info["model_in_list"])
            },
            torch.tensor(2.7774928, dtype=torch.double),
        ),
        (
            {
                key: test_x[2][idx]
                for idx, key in enumerate(model_info["model_in_list"])
            },
            torch.tensor(2.792812, dtype=torch.double),
        ),
    ],
)
def test_california_housing_model_execution_diff_values(test_input, expected):
    cal_model = PyTorchModel(**model_kwargs)

    results = cal_model.evaluate(test_input)

    assert torch.isclose(results["MedHouseVal"], expected)
    assert_variables_updated(
        test_input["HouseAge"].item(),
        expected.item(),
        cal_model,
        "HouseAge",
        "MedHouseVal",
    )


def test_california_housing_model_execution_no_transformation():
    # if we don't pass in an output transformer, we expect to get the untransformed
    # result back
    new_kwargs = deepcopy(model_kwargs)
    new_kwargs["output_transformers"] = []
    cal_model = PyTorchModel(**new_kwargs)

    results = cal_model.evaluate(test_x_dict)

    assert torch.isclose(
        results["MedHouseVal"], torch.tensor(1.8523695, dtype=torch.double)
    )
    assert_variables_updated(
        test_x_dict["HouseAge"].item(), 1.8523695, cal_model, "HouseAge", "MedHouseVal"
    )


def test_california_housing_model_execution_missing_input(caplog):
    cal_model = PyTorchModel(**model_kwargs)

    missing_dict = deepcopy(test_x_dict)
    del missing_dict["Longitude"]

    results = cal_model.evaluate(missing_dict)

    assert len(caplog.records) == 1
    assert caplog.records[0].levelname == "WARNING"
    assert (
        caplog.records[0].message
        == "'Longitude' missing from input_dict, using default value"
    )


def test_differentiability():
    cal_model = PyTorchModel(**model_kwargs)

    differentiable_dict = deepcopy(test_x_dict)
    for value in differentiable_dict.values():
        value.requires_grad = True

    results = cal_model.evaluate(differentiable_dict)

    # if we maintain differentiability, we should be able to call .backward()
    # on a model output without it causing an error
    for key, value in results.items():
        try:
            value.backward()
            assert value.requires_grad == True
        except AttributeError as exc:
            # if the attribute error is raised because we're, returning a float,
            # the test should fail
            assert False, "'float' object has no attribute 'backward'"

    # we also want to make sure that the input_variable and output_variable
    # values are still treated as floats
    assert isinstance(results["MedHouseVal"], torch.Tensor)
    assert torch.isclose(
        results["MedHouseVal"], torch.tensor(4.063651, dtype=torch.double)
    )
    assert_variables_updated(
        test_x_dict["HouseAge"].item(), 4.063651, cal_model, "HouseAge", "MedHouseVal"
    )
