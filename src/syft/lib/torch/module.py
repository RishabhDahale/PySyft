# stdlib
import ast
from collections import OrderedDict
import copy
import os
from pathlib import Path
import sys
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from typing import Union

# third party
import torch

# syft absolute
import syft as sy

# syft relative
from ...generate_wrapper import GenerateWrapper

# from ...core.pointer.pointer import Pointer
from ...lib.util import full_name_with_qualname
from ...logger import critical
from ...logger import info
from ...logger import traceback_and_raise
from ...proto.lib.torch.module_pb2 import Module as Module_PB
from ..python.collections import OrderedDict as SyOrderedDict

# from ...core.node.common.service.auth import AuthorizationException


def repr_to_kwargs(repr_str: str) -> Tuple[List[Any], Dict[Any, Any]]:
    # for example: repr_str = Conv2d(...).extra_repr()
    # produces:    > str("1, 32, kernel_size=(3, 3), stride=(1, 1)")
    # then we just have to split it into args and kwargs
    # using ast.literal_eval we can use python to give us the real primitive types
    # from the strings in a safe way
    # str("1 ") becomes int(1)
    # str("(1, 2) ") becomes tuple(1, 2)
    args: List[Any] = []
    kwargs: Dict[Any, Any] = {}
    parts = repr_str.split(",")

    # tuples are split by commas as well, so we will keep a tab on open parentheses
    # then concat with "," until we find a close parentheses
    # TODO: make work nested with a count and add tests
    para_open = False
    buffer = ""
    for part in parts:
        try:
            if "(" in part:
                para_open = True
                buffer = ""
            if para_open is True:
                buffer += part + ","
                if ")" in part:
                    # remove trailing ,
                    part = buffer[:-1]
                    buffer = ""
                    para_open = False
                else:
                    continue

            string = part.strip()
            if "=" not in string:
                # its an arg
                arg = ast.literal_eval(string)
                args.append(arg)
            else:
                # its a kwarg
                kv = string.split("=")
                key = str(kv[0])
                string = kv[1].strip()
                value = ast.literal_eval(string)
                kwargs[key.strip()] = value
        except Exception as e:
            info(f"ast.literal_eval failed to parse part: {string}. {e}")

    return (args, kwargs)


class Module:
    """
    This is our equivalent of torch.nn.Module and aims to have the same external
    interface. We will need to support both torch Modules and Module Pointers.
    """

    def __init__(self, torch_ref: Any) -> None:
        self.setup(torch_ref=torch_ref)

    def setup(self, torch_ref: Any) -> None:
        # the remote torch means the model is remote
        self.remote_model: Optional["Module"] = None
        self.local_model: Optional["Module"] = None
        self.duet = None
        if "syft" in full_name_with_qualname(klass=type(torch_ref)):
            info("> Creating remote model")
            self.is_local = False
        else:
            # otherwise we have a local model
            info("> Creating local model")
            self.is_local = True

        self.torch_ref = torch_ref
        self.training = False
        self._modules: OrderedDict[str, Module] = OrderedDict()
        real_module = torch_ref.nn.Module()
        self.__dict__["real_module"] = real_module  # bypass getattr/setattr
        # if issubclass(type(real_module), Pointer):
        #     try:
        #         # TODO: this needs fixing but should be on by default for now
        #         # https://github.com/OpenMined/PySyft/issues/5242
        #         real_module.searchable = True
        #     except AuthorizationException as e:
        #         print(f"Cant make real_module searchable. {e}")

    def __setattr__(self, name: str, value: Union[Any, "Module"]) -> None:
        if isinstance(value, torch.nn.Module):
            modules = self.__dict__.get("_modules")
            if modules is not None:
                modules[name] = value

            # attach all the sub modules to a real module so that we can have a
            # remote module pointer that acts like a real model
            real_module: Optional[OrderedDict] = self.__dict__.get("real_module")
            if real_module is not None:
                real_module.add_module(name, value)  # type: ignore
        else:
            object.__setattr__(self, name, value)

    def __getattr__(self, name: str) -> Union[Any, "Module"]:
        modules: Optional[OrderedDict] = self.__dict__.get("_modules")
        if modules is not None:
            if name in modules:
                return modules[name]

        return object.__getattribute__(self, name)

    def train(self, mode: bool = True) -> "Module":
        self.training = mode
        for _, module in self.modules.items():
            module.train(mode)
        return self

    def eval(self) -> "Module":
        return self.train(False)

    def __call__(
        self, *args: Union[List[Any], Tuple[Any, ...]], **kwargs: Dict[Any, Any]
    ) -> Any:
        return self.forward(*args, **kwargs)

    @property
    def modules(self) -> OrderedDict:
        modules = self.__dict__.get("_modules")
        if modules is not None:
            return modules
        return OrderedDict()

    # local list of remote ListPointers of TensorPointers
    def parameters(self, recurse: bool = True) -> Optional[List[Any]]:
        params_list: Optional[List[Any]] = None

        if self.is_local is True:
            # we are local so use normal torch params
            params_list = []
        for _, module in self.modules.items():
            params = module.parameters(recurse)
            if params_list is None:
                # only on remote create a remote list so we can concat the param list
                # pointers without having to actually get them
                self.duet = params.client
                params_list = self.duet.syft.lib.python.List()  # type: ignore
            # either way lets concat them until we have a big list of parameters
            params_list += params
        return params_list

    def cuda(self, device: Any) -> "Module":
        for _, module in self.modules.items():
            module.cuda(device)
        return self

    def cpu(self) -> "Module":
        for _, module in self.modules.items():
            module.cpu()
        return self

    def load_state_dict(self, input: Union[str, os.PathLike, Dict[str, Any]]) -> None:
        if not self.is_local:
            info("> This model is remote so try calling .get()")
            return None

        state_dict = {}
        if isinstance(input, (str, os.PathLike)):
            with open(Path(input), "rb") as f:
                state_dict = torch.load(f)
        else:
            state_dict = dict(input)

        if not issubclass(type(state_dict), dict):
            traceback_and_raise(
                f"  Invalid input: {type(input)}. "
                + "Try inputting a state_dict or .pth file."
            )

        info("> Loading model weights")
        layers: Dict[str, Any] = {}
        for save_key, values in state_dict.items():
            parts = save_key.split(".")
            if len(parts) < 2:
                info(f"  state dict key is too short: {save_key}")
                continue
            layer = parts[0]
            attr = parts[1]
            if layer not in layers:
                layers[layer] = {}
            layers[layer][attr] = values

        for layer, sd in layers.items():
            local_layer = getattr(self, layer, None)
            if local_layer is not None and hasattr(local_layer, "load_state_dict"):
                d = local_layer.load_state_dict(sd)
                info(f"  {layer} state dict loaded with: {d}")
            else:
                info(f"  Model doesnt have layer {layer}")

        info("> Finished loading weights")
        return None

    def state_dict(self) -> Optional[Dict[str, Any]]:
        if not self.is_local:
            info("> This model is remote so try calling .get()")
            return None

        info("> Saving model weights")
        model_state_dict = OrderedDict()
        for name, module in self.modules.items():
            if hasattr(module, "state_dict"):
                for k, v in module.state_dict().items():
                    save_key = f"{name}.{k}"
                    model_state_dict[save_key] = v

        info("> Finished saving weights")
        return model_state_dict

    def save(self, path: Union[str, bytes, os.PathLike]) -> None:
        if not self.is_local:
            info("> This model is remote so try calling .get()")
            return

        state_dict = self.state_dict()
        torch.save(state_dict, path)

    def load(self, path: Union[str, os.PathLike]) -> None:
        if not self.is_local:
            info("> This model is remote so try calling .get()")
            return

        self.load_state_dict(input=path)

    def send(self, client: Any, send_parameters: bool = True) -> Any:
        if not self.is_local:
            info("> This model is remote so try calling .get()")
            return

        info("> Sending local model")

        remote_model = copy.copy(self)
        remote_model.setup(torch_ref=client.torch)
        remote_model.duet = client

        for name, module in self.modules.items():
            fqn = full_name_with_qualname(klass=type(module))
            klass = client.lib_ast.query(fqn, obj_type=type(module))
            module_repr = module.extra_repr()
            args, kwargs = repr_to_kwargs(repr_str=module_repr)
            remote_module_ptr = klass(*args, **kwargs)
            remote_model.__setattr__(name, remote_module_ptr)

            # if the remote module has state_dict lets get it
            if (
                send_parameters
                and hasattr(module, "state_dict")
                and hasattr(remote_module_ptr, "load_state_dict")
            ):
                local_state_ord_dict = module.state_dict()
                # cast to dict because OrderedDict is not supported

                # get a blocking copy of the state_dict
                info(f"  Sending local layer: {name}")
                # cant import Dict / PrimitiveFactory due to circular imports
                remote_state_dict_ptr = client.syft.lib.python.Dict(
                    dict(local_state_ord_dict)
                )
                # iterate through the key, values
                # weights and biases should be in there
                remote_module_ptr.load_state_dict(remote_state_dict_ptr)

        info("\n> Finished sending local model <\n\n")
        self.remote_model = remote_model
        return self.remote_model

    def get(
        self,
        request_block: bool = False,
        timeout_secs: int = 20,
        reason: str = "",
        delete_obj: bool = False,
    ) -> Optional["Module"]:

        if self.is_local:
            info("> This model is local. Maybe you meant to call .send()?")
            return None

        info("> Downloading remote model")

        local_model = copy.copy(self)
        local_model.setup(torch_ref=torch)
        local_model.duet = self.duet

        for layer_name, module in self.modules.items():
            module_parts = module.path_and_name.split(".")
            klass_name = module_parts.pop()
            klass = getattr(sys.modules[".".join(module_parts)], klass_name)
            repr_ptr = module.extra_repr()

            module_repr = repr_ptr.get(
                request_block=request_block,
                reason=reason,
                timeout_secs=timeout_secs,
            )

            if module_repr is None:
                info(f"  Request for {reason} extra_repr failed, skipping layer")
                continue

            args, kwargs = repr_to_kwargs(repr_str=module_repr.upcast())
            local_module = klass(*args, **kwargs)

            # the local real module has been set on the sy module
            local_model.__setattr__(layer_name, local_module)

            try:
                # if the remote module has state_dict lets get it
                if hasattr(module, "state_dict") and hasattr(
                    local_module, "load_state_dict"
                ):
                    info("loading remote state dict")
                    sd_ptr = module.state_dict()
                    # get a blocking copy of the state_dict
                    info(f"  Downloading remote layer: {layer_name}")
                    state_dict = sd_ptr.get(
                        request_block=request_block,
                        reason=reason,
                        timeout_secs=timeout_secs,
                        delete_obj=delete_obj,
                    )
                    # We have to recreate the OrderedDict for load_state_dict to work
                    ordered_state_dict = OrderedDict()
                    for elem, item in state_dict.items():
                        ordered_state_dict[str(elem)] = item
                    # iterate through the key, values
                    # weights and biases should be in there
                    if state_dict is not None:
                        # TODO: support torch.nn.modules.module._IncompatibleKeys
                        local_module.load_state_dict(ordered_state_dict)
                    else:
                        info(
                            f"  Failed to get {layer_name} state_dict, skipping layer."
                        )

            except Exception as e:
                critical(f"  Failed to download remote state for {layer_name}.")
                traceback_and_raise(e)

        info("\n> Finished downloading remote model <\n\n")
        self.local_model = local_model
        return self.local_model

    # zero them so we know they are copied
    def zero_layers(self) -> None:
        for m in self.modules.values():
            if hasattr(m, "weight"):
                m.weight.requires_grad_(False).zero_()
            if hasattr(m, "bias"):
                m.bias.requires_grad_(False).zero_()

    # easy way to check the weights have changed
    def debug_sum_layers(self) -> None:
        info("> Summing layers for debugging: ")
        for n, m in self.modules.items():
            if hasattr(m, "state_dict"):
                if self.is_local:
                    state_dict = m.state_dict()
                else:
                    state_dict = m.state_dict().get()

                for k, v in state_dict.items():
                    if hasattr(v, "sum"):
                        s = v.sum().item()
                        info(f"  Layer {n} sum({k}): {s}")


def object2proto(obj: torch.nn.Module, is_child: bool = False) -> Module_PB:
    proto = Module_PB()
    if "torch.nn." in type(obj).__module__:
        proto.module_type = type(obj).__name__
    else:
        proto.module_type = f"_USER_DEFINED_MODULE_{type(obj).__name__}"

    proto.module_repr = obj.extra_repr()

    if not is_child:
        proto.state_dict.CopyFrom(sy.serialize(SyOrderedDict(obj.state_dict())))

    for n, m in obj.named_children():
        child_proto = object2proto(m, is_child=True)
        child_proto.module_name = n
        proto.children.append(child_proto)

    return proto


def proto2object(proto: Module_PB) -> torch.nn.Module:
    is_userdefined = proto.module_type.startswith("_USER_DEFINED_MODULE_")

    if is_userdefined:
        obj_type = type(
            proto.module_type.replace("_USER_DEFINED_MODULE_", ""),
            (torch.nn.Module,),
            {},
        )
    else:
        obj_type = getattr(torch.nn, proto.module_type)

    args, kwargs = repr_to_kwargs(repr_str=proto.module_repr)
    obj = obj_type(*args, **kwargs)

    for child_proto in proto.children:
        obj.add_module(child_proto.module_name, sy.deserialize(child_proto))

    if proto.state_dict.ByteSize() > 0:
        obj.load_state_dict(sy.deserialize(proto.state_dict))

    return obj


GenerateWrapper(
    wrapped_type=torch.nn.Module,
    import_path="torch.nn.Module",
    protobuf_scheme=Module_PB,
    type_object2proto=object2proto,
    type_proto2object=proto2object,
)
