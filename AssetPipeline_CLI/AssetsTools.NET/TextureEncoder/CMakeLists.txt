add_library (TextureEncoder SHARED "TextureEncoder.cpp" "TextureEncoder.h" "TextureFormat.h")
set_target_properties(TextureEncoder PROPERTIES OUTPUT_NAME "textureencoder")

if (WIN32 AND NOT PVRTL_ENABLED)
    # Cuttlefish's Windows binary imports a small PVRTexLib surface even when
    # ETC2/ASTC encoding is used. Build the repository's no-op compatibility
    # stub instead of distributing the separately licensed runtime.
    add_library(PVRTexLibStub SHARED "pvrtlstub.c")
    set_target_properties(PVRTexLibStub PROPERTIES OUTPUT_NAME "PVRTexLib")
    add_dependencies(TextureEncoder PVRTexLibStub)
endif()

if (CMAKE_VERSION VERSION_GREATER 3.12)
    set_property(TARGET TextureEncoder PROPERTY CXX_STANDARD 20)
endif()

if (WIN32)
    set(LIB_DIR "${CMAKE_SOURCE_DIR}/NativeLibs/cuttlefish/windows/lib")
    set(BIN_DIR "${CMAKE_SOURCE_DIR}/NativeLibs/cuttlefish/windows/bin")
    set(LIB_PREFIX "")
    set(LIB_SUFFIX ".lib")
else()
    set(LIB_DIR "${CMAKE_SOURCE_DIR}/NativeLibs/cuttlefish/linux/lib")
    set(BIN_DIR "${CMAKE_SOURCE_DIR}/NativeLibs/cuttlefish/linux/bin")
    set(LIB_PREFIX "lib")
    set(LIB_SUFFIX ".so")
endif()

#link_directories(${LIB_DIR})
if (WIN32)
    target_include_directories(TextureEncoder PRIVATE "${CMAKE_SOURCE_DIR}/NativeLibs/cuttlefish/windows/include")
else()
    target_include_directories(TextureEncoder PRIVATE "${CMAKE_SOURCE_DIR}/NativeLibs/cuttlefish/linux/include")
endif()

target_link_libraries(TextureEncoder ${LIB_DIR}/${LIB_PREFIX}cuttlefish${LIB_SUFFIX})
if (PVRTL_ENABLED)
    target_link_libraries(TextureEncoder ${LIB_DIR}/${LIB_PREFIX}PVRTexLib${LIB_SUFFIX})
endif()

if (WIN32)
    set(DEP_DLLS_TO_BIN "cuttlefish.dll")
    if(PVRTL_ENABLED)
      list(APPEND DEP_DLLS_TO_BIN "PVRTexLib.dll")
    endif()
    foreach(DLL ${DEP_DLLS_TO_BIN})
        add_custom_command(TARGET TextureEncoder POST_BUILD
            COMMAND ${CMAKE_COMMAND} -E copy_if_different
            "${BIN_DIR}/${DLL}"
            $<TARGET_FILE_DIR:TextureEncoder>)
    endforeach()
endif()
