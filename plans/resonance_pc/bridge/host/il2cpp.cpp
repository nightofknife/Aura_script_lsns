#include "il2cpp.hpp"
#include <cstring>
namespace bridge {
Il2Cpp il;
std::atomic<bool> process_exiting{false};
thread_local void *attached_thread{};
template <class T> void symbol(HMODULE m, T &p, const char *name) {
  p = reinterpret_cast<T>(GetProcAddress(m, name));
  if (!p)
    throw Error("version_mismatch", std::string("Missing export ") + name);
}
void Il2Cpp::load() {
  module = GetModuleHandleW(L"GameAssembly.dll");
  if (!module)
    throw Error("version_mismatch", "GameAssembly not loaded");
#define API(member, name) symbol(module, member, "il2cpp_" name)
  API(domain_, "domain_get");
  API(assemblies_, "domain_get_assemblies");
  API(image_, "assembly_get_image");
  API(class_, "class_from_name");
  API(method_, "class_get_method_from_name");
  API(parent_, "class_get_parent");
  API(invoke_, "runtime_invoke");
  API(new_, "object_new");
  API(class_type_, "class_get_type");
  API(type_object_, "type_get_object");
  API(string_, "string_new");
  API(object_class_, "object_get_class");
  API(assignable_, "class_is_assignable_from");
  API(field_find_, "class_get_field_from_name");
  API(field_get_, "field_get_value");
  API(field_set_, "field_set_value");
  API(field_offset_, "field_get_offset");
  API(param_, "method_get_param");
  API(type_class_, "class_from_type");
  API(unbox_, "object_unbox");
  API(resolve_, "resolve_icall");
  API(thread_attach_, "thread_attach");
  API(keep_, "gchandle_new");
  API(free_, "gchandle_free");
  API(target_, "gchandle_get_target");
  API(thread_detach_, "thread_detach");
  API(array_, "array_new");
  API(value_size_, "class_value_size");
  API(virtual_, "object_get_virtual_method");
  API(methods_, "class_get_methods");
  API(method_name_, "method_get_name");
  API(param_count_, "method_get_param_count");
  API(class_name_, "class_get_name");
  API(class_namespace_, "class_get_namespace");
#undef API
}
void Il2Cpp::attach() { attached_thread = thread_attach_(domain_()); }
void Il2Cpp::detach() {
  if (attached_thread) {
    thread_detach_(attached_thread);
    attached_thread = nullptr;
  }
}
Class Il2Cpp::klass(const char *ns, const char *n) {
  size_t count{};
  auto all = assemblies_(domain_(), &count);
  for (size_t i = 0; i < count; i++)
    if (auto c = class_(image_(all[i]), ns, n))
      return c;
  throw Error("version_mismatch", std::string("Missing class ") + ns + "." + n);
}
Method Il2Cpp::method(Class c, const char *n, int argc) {
  for (; c; c = parent_(c))
    if (auto m = method_(c, n, argc))
      return m;
  throw Error("version_mismatch", std::string("Missing method ") + n);
}
Method Il2Cpp::typed_method(Class c, const char *name, std::initializer_list<const char *> types) {
  for (; c; c = parent_(c)) {
    void *iterator{};
    while (auto m = methods_(c, &iterator)) {
      if (strcmp(method_name_(m), name) != 0 || param_count_(m) != types.size())
        continue;
      bool match = true;
      uint32_t i = 0;
      for (auto expected : types) {
        auto k = type_class_(param_(m, i++));
        if (!k || std::string(class_namespace_(k)) + "." + class_name_(k) != expected) {
          match = false;
          break;
        }
      }
      if (match)
        return m;
    }
  }
  throw Error("version_mismatch", std::string("Missing typed overload ") + name);
}
Method Il2Cpp::method(Obj o, const char *n, int argc, bool) {
  return method(object_class_(o), n, argc);
}
Obj Il2Cpp::invoke(Method m, Obj o, std::initializer_list<void *> a) {
  Obj exc{};
  std::vector<void *> args(a);
  Obj r = invoke_(m, o, args.data(), &exc);
  if (exc)
    throw Error("unity_exception", "Unity invocation raised a managed exception");
  return r;
}
Obj Il2Cpp::call(Obj o, const char *n, std::initializer_list<void *> a) {
  if (!o)
    throw Error("unity_null_target", n);
  return invoke(method(o, n, int(a.size()), true), o, a);
}
Obj Il2Cpp::call_interface(Obj o, Class iface, const char *n, std::initializer_list<void *> a) {
  if (!o)
    throw Error("unity_null_target", n);
  auto m = virtual_(o, method(iface, n, int(a.size())));
  if (!m)
    throw Error("drag_target_missing", "Interface method missing");
  return invoke(m, o, a);
}
Obj Il2Cpp::stat(Class c, const char *n, std::initializer_list<void *> a) {
  return invoke(method(c, n, int(a.size())), nullptr, a);
}
Obj Il2Cpp::create(Class c) { return new_(c); }
Obj Il2Cpp::type(Class c) { return type_object_(class_type_(c)); }
Obj Il2Cpp::string(const char *s) { return string_(s); }
std::string Il2Cpp::utf8(Obj s) {
  if (!s)
    return {};
  auto len = *reinterpret_cast<int *>(static_cast<char *>(s) + 16);
  auto p = reinterpret_cast<wchar_t *>(static_cast<char *>(s) + 20);
  int n = WideCharToMultiByte(CP_UTF8, 0, p, len, nullptr, 0, nullptr, nullptr);
  std::string r(n, '\0');
  WideCharToMultiByte(CP_UTF8, 0, p, len, r.data(), n, nullptr, nullptr);
  return r;
}
bool Il2Cpp::alive(Obj o) {
  if (!o)
    return false;
  return scalar<bool>(stat(klass("UnityEngine", "Object"), "op_Implicit", {o}));
}
bool Il2Cpp::derives(Obj o, Class c) { return o && assignable_(c, object_class_(o)); }
void *Il2Cpp::field_(Class c, const char *n) {
  for (; c; c = parent_(c))
    if (auto f = field_find_(c, n))
      return f;
  throw Error("version_mismatch", std::string("Missing field ") + n);
}
int Il2Cpp::offset(Class c, const char *n) { return int(field_offset_(field_(c, n))); }
Obj Il2Cpp::object_class(Obj o) { return object_class_(o); }
Class Il2Cpp::param_class(Method m, int index) { return type_class_(param_(m, index)); }
size_t Il2Cpp::value_size(Class c) {
  uint32_t align{};
  return value_size_(c, &align);
}
void *Il2Cpp::unbox(Obj o) { return unbox_(o); }
void *Il2Cpp::icall(const char *n) {
  auto p = resolve_(n);
  if (!p)
    throw Error("version_mismatch", std::string("Missing icall ") + n);
  return p;
}
uint32_t Il2Cpp::keep(Obj o) { return keep_(o, false); }
void Il2Cpp::free(uint32_t h) { free_(h); }
Obj Il2Cpp::target(uint32_t h) { return target_(h); }
Obj Il2Cpp::array(Class c, size_t n) { return array_(c, n); }
} // namespace bridge
