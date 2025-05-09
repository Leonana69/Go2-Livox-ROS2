cmake_minimum_required(VERSION 3.8)
project(typego_sdk)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

# find dependencies
find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(tf2_ros REQUIRED)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  # the following line skips the linter which checks for copyrights
  # comment the line when a copyright and license is added to all source files
  set(ament_cmake_copyright_FOUND TRUE)
  # the following line skips cpplint (only works in a git repo)
  # comment the line when this package is in a git repo and when
  # a copyright and license is added to all source files
  set(ament_cmake_cpplint_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

add_executable(livox_udp_receiver_node src/livox_udp_receiver.cpp)
ament_target_dependencies(livox_udp_receiver_node rclcpp sensor_msgs tf2_ros)

pkg_check_modules(GST REQUIRED gstreamer-1.0 gstreamer-app-1.0)

add_executable(gstreamer_receiver_node src/gstreamer_receiver.cpp)
ament_target_dependencies(gstreamer_receiver_node rclcpp sensor_msgs)

target_include_directories(gstreamer_receiver_node PRIVATE ${GST_INCLUDE_DIRS})
target_link_libraries(gstreamer_receiver_node ${GST_LIBRARIES})

install(TARGETS
  livox_udp_receiver_node
  gstreamer_receiver_node
  DESTINATION lib/${PROJECT_NAME})

ament_package()
