#include <torch/extension.h>

namespace torch_ort {
namespace eager {

void GenerateCustomOpsBindings(pybind11::module module);

} // namespace eager
} // namespace torch_ort