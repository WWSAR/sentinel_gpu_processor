#ifndef GPU_DEVICE_HPP
#define GPU_DEVICE_HPP

#include <cstdlib>
#include <iostream>
#include <string>

#include <cuda_runtime.h>

/**
 * Set the active CUDA device for subsequent operations.
 *
 * If ``device_id`` is negative, the function returns without changing the
 * current device.  When ``device_id`` exceeds the number of available GPUs,
 * the function falls back to device 0 after printing a warning.
 *
 * Parameters
 * ----------
 * device_id : int
 *     Zero-based index of the CUDA device to activate.
 */
inline void set_gpu(int device_id) {
    if (device_id < 0) return;

    int device_count;
    cudaError_t err = cudaGetDeviceCount(&device_count);
    if (err != cudaSuccess) {
        std::cerr << "Warning: Cannot query CUDA device count: "
                  << cudaGetErrorString(err) << std::endl;
        return;
    }

    if (device_id >= device_count) {
        std::cerr << "Warning: Requested GPU device " << device_id
                  << " but only " << device_count << " device(s) available. "
                  << "Falling back to device 0." << std::endl;
        device_id = 0;
    }

    err = cudaSetDevice(device_id);
    if (err != cudaSuccess) {
        std::cerr << "Error: Failed to set CUDA device to " << device_id
                  << ": " << cudaGetErrorString(err) << std::endl;
        exit(1);
    }

    cudaDeviceProp prop;
    cudaGetDeviceProperties(&prop, device_id);
    std::cout << "Using GPU device " << device_id << ": " << prop.name
              << std::endl;
}

/**
 * Scan command-line arguments for ``--gpu <id>`` and return the parsed
 * device id.  Returns -1 if the flag is absent so callers can distinguish
 * "not set" from "explicitly set to 0".
 *
 * Parameters
 * ----------
 * argc : int
 *     Argument count (from main).
 * argv : char**
 *     Argument vector (from main).
 *
 * Returns
 * -------
 * device_id : int
 *     The requested GPU index, or -1 if ``--gpu`` was not found.
 */
inline int parse_gpu_arg(int argc, char *argv[]) {
    for (int i = 1; i < argc - 1; ++i) {
        std::string arg(argv[i]);
        if (arg == "--gpu" || arg == "-g") {
            return std::stoi(argv[i + 1]);
        }
    }
    return -1;
}

#endif  // GPU_DEVICE_HPP
