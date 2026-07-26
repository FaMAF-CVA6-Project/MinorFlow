import os
import subprocess
from concurrent.futures import ProcessPoolExecutor, as_completed

# ==========================================
# Configuration
# ==========================================
# Adjust this number based on your CPU cores and available RAM.
MAX_WORKERS = 4
TRACER_SCRIPT = "../MinorFlow_tracer.py"


def process_trace(txt_file):
    """
    Executes the MinorFlow tracer for a single text trace file.
    It formats the output JSON name by removing '_trace' from the base name.
    """
    # Extract the base name and format it.
    # Example: "program_trace.configX.txt" -> "program.configX"
    base_name = txt_file.rsplit('.txt', 1)[0]
    clean_base_name = base_name.replace('_trace', '')
    json_file = f"{clean_base_name}.json"

    # Prepare the exact command requested
    cmd = [
        "python3", TRACER_SCRIPT,
        txt_file,
        "-o", json_file
    ]

    try:
        # Execute the subprocess
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return f"[SUCCESS] Generated: {json_file}"
    except subprocess.CalledProcessError as e:
        # If the tracer fails, catch the error without stopping other processes
        return f"[ERROR] Failed on {txt_file}:\n{e.stderr}"


def main():
    # List all files in the current directory ending in .txt
    # We assume trace files end with .txt as per the provided example
    trace_files = [f for f in os.listdir(
        '.') if f.endswith('.txt') and '_trace' in f]

    if not trace_files:
        print("No valid trace .txt files found in the current directory.")
        return

    print(f"Found {len(trace_files)} trace files to process.")
    print(f"Starting concurrent processing with {MAX_WORKERS} workers...\n")

    # Use ProcessPoolExecutor to handle concurrency
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as executor:
        # Submit all tasks to the pool
        futures = {executor.submit(process_trace, trace)
                                   : trace for trace in trace_files}

        # Print results as each task completes
        for future in as_completed(futures):
            print(future.result())

    print("\nBatch processing finished.")


if __name__ == "__main__":
    main()
