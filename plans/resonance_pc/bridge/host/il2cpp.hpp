#pragma once
#include <atomic>
#include <cstdint>
#include <initializer_list>
#include <stdexcept>
#include <string>
#include <vector>
#include <windows.h>

namespace bridge {
using Obj = void *;
using Class = void *;
using Method = const void *;
struct Vec2 {
  float x{}, y{};
};
struct Vec3 {
  float x{}, y{}, z{};
};
struct Error : std::runtime_error {
  std::string code;
  Error(std::string c, std::string m) : std::runtime_error(m), code(std::move(c)) {}
};
class Il2Cpp {
public:
  HMODULE module{};
  void load();
  void attach();
  void detach();
  Class klass(const char *ns, const char *name);
  Method method(Class c, const char *name, int argc);
  Method typed_method(Class c, const char *name,
                      std::initializer_list<const char *> parameter_types);
  Method method(Obj o, const char *name, int argc, bool object);
  Obj invoke(Method m, Obj target, std::initializer_list<void *> args = {});
  Obj call(Obj target, const char *name, std::initializer_list<void *> args = {});
  Obj call_interface(Obj target, Class iface, const char *name,
                     std::initializer_list<void *> args = {});
  Obj stat(Class c, const char *name, std::initializer_list<void *> args = {});
  Obj create(Class c);
  Obj type(Class c);
  Obj string(const char *s);
  std::string utf8(Obj s);
  bool alive(Obj o);
  bool derives(Obj o, Class c);
  int offset(Class c, const char *field);
  Obj object_class(Obj o);
  Class param_class(Method m, int index);
  size_t value_size(Class c);
  void *unbox(Obj o);
  void *icall(const char *n);
  uint32_t keep(Obj o);
  void free(uint32_t h);
  Obj target(uint32_t h);
  Obj array(Class element, size_t n);
  size_t length(Obj a) { return *reinterpret_cast<size_t *>(static_cast<char *>(a) + 24); }
  void *data(Obj a) { return static_cast<char *>(a) + 32; }
  void *pointer(Method m) { return *reinterpret_cast<void *const *>(m); }
  template <class T> T scalar(Obj box) {
    if (!box)
      throw Error("unity_null_result", "Expected boxed value");
    return *static_cast<T *>(unbox(box));
  }
  template <class T> T get(Obj o, const char *field) {
    T v{};
    field_get_(o, field_(object_class_(o), field), &v);
    return v;
  }
  template <class T> void set(Obj o, const char *field, T v) {
    field_set_(o, field_(object_class_(o), field), &v);
  }

private:
  void *field_(Class c, const char *name);
  void *(*domain_)(){};
  const void **(*assemblies_)(void *, size_t *){};
  void *(*image_)(const void *){};
  Class (*class_)(void *, const char *, const char *){};
  Method (*method_)(Class, const char *, int){};
  Class (*parent_)(Class){};
  Method (*methods_)(Class, void **){};
  const char *(*method_name_)(Method){};
  uint32_t (*param_count_)(Method){};
  const char *(*class_name_)(Class){};
  const char *(*class_namespace_)(Class){};
  Obj (*invoke_)(Method, Obj, void **, Obj *){};
  Obj (*new_)(Class){};
  const void *(*class_type_)(Class){};
  Obj (*type_object_)(const void *){};
  Obj (*string_)(const char *){};
  Class (*object_class_)(Obj){};
  bool (*assignable_)(Class, Class){};
  void *(*field_find_)(Class, const char *){};
  void (*field_get_)(Obj, void *, void *){};
  void (*field_set_)(Obj, void *, void *){};
  size_t (*field_offset_)(void *){};
  const void *(*param_)(Method, uint32_t){};
  Class (*type_class_)(const void *){};
  void *(*unbox_)(Obj){};
  void *(*resolve_)(const char *){};
  void *(*thread_attach_)(void *){};
  void (*thread_detach_)(void *){};
  uint32_t (*keep_)(Obj, bool){};
  void (*free_)(uint32_t){};
  Obj (*target_)(uint32_t){};
  Obj (*array_)(Class, size_t){};
  int32_t (*value_size_)(Class, uint32_t *){};
  Method (*virtual_)(Obj, Method){};
};
extern Il2Cpp il;
extern std::atomic<bool> process_exiting;
class Root {
  uint32_t handle_{};

public:
  Root() = default;
  explicit Root(Obj o) : handle_(o ? il.keep(o) : 0) {}
  ~Root() {
    if (!process_exiting)
      reset();
  }
  Root(const Root &) = delete;
  Root &operator=(const Root &) = delete;
  Root(Root &&r) noexcept : handle_(r.handle_) { r.handle_ = 0; }
  Root &operator=(Root &&r) noexcept {
    reset();
    handle_ = r.handle_;
    r.handle_ = 0;
    return *this;
  }
  Obj get() const { return handle_ ? il.target(handle_) : nullptr; }
  void reset(Obj o = nullptr) {
    if (handle_)
      il.free(handle_);
    handle_ = o ? il.keep(o) : 0;
  }
};
} // namespace bridge
