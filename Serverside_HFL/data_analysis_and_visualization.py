import pandas as pd
import matplotlib.pyplot as plt
import os
from pathlib import Path

def load_data(log_dir):
    """Load log data from the specified directory."""
    log_files = Path(log_dir).glob("*.log")
    data = []
    for log_file in log_files:
        with open(log_file, 'r') as f:
            for line in f:
                # Assuming logs are in JSON format for simplicity
                try:
                    record = eval(line.strip())  # Replace eval with json.loads if JSON
                    data.append(record)
                except Exception as e:
                    print(f"Failed to parse line in {log_file}: {e}")
    return pd.DataFrame(data)

def visualize_data(df):
    """Visualize the data using matplotlib."""
    if df.empty:
        print("No data to visualize.")
        return

    # Example: Plot request durations
    if 'duration_s' in df.columns:
        plt.hist(df['duration_s'], bins=20, color='blue', alpha=0.7)
        plt.title('Request Duration Distribution')
        plt.xlabel('Duration (s)')
        plt.ylabel('Frequency')
        plt.show()
    else:
        print("Column 'duration_s' not found in data.")

def analyze_data(df):
    """Perform basic analysis on the data."""
    if df.empty:
        print("No data to analyze.")
        return

    # Example: Calculate basic statistics for request durations
    if 'duration_s' in df.columns:
        print("Request Duration Statistics:")
        print(df['duration_s'].describe())
    else:
        print("Column 'duration_s' not found in data.")

if __name__ == "__main__":
    log_directory = "./logs/time_records"
    if not os.path.exists(log_directory):
        print(f"Log directory '{log_directory}' does not exist.")
    else:
        data = load_data(log_directory)
        analyze_data(data)
        visualize_data(data)