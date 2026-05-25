import java.io.File;
import java.io.FileWriter;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

public class TestLogWriter {
    public static void main(String[] args) {
        String dirPath = "C:\\Users\\tetsu\\AndroidStudioProjects\\HFL_experiment\\data\\data\\com.example.hfl_experiment\\files";
        File dir = new File(dirPath);
        if (!dir.exists()) {
            boolean ok = dir.mkdirs();
            System.out.println("Created dir: " + dir.getAbsolutePath() + " -> " + ok);
        } else {
            System.out.println("Dir exists: " + dir.getAbsolutePath());
        }
        File logFile = new File(dir, "training_events.log");
        SimpleDateFormat sdf = new SimpleDateFormat("yyyy-MM-dd HH:mm:ss.SSS", Locale.getDefault());
        try {
            boolean isNew = false;
            if (!logFile.exists()) {
                isNew = logFile.createNewFile();
            }
            FileWriter fw = new FileWriter(logFile, true);
            if (isNew) {
                fw.append("timestamp,event,round,extra\n");
            }
            String ts = sdf.format(new Date());
            // write a variety of events to simulate TrainingManager behavior
            fw.append(String.format("%s,training_start,1,%s\n", ts, ""));
            fw.append(String.format("%s,epoch,1,%s\n", ts, "epoch=1 loss=0.123 acc=85.00%"));
            fw.append(String.format("%s,training_complete,1,%s\n", ts, "sha=testsha"));
            fw.append(String.format("%s,upload_start,1,%s\n", ts, ""));
            fw.append(String.format("%s,upload_complete,1,%s\n", ts, "nSamples=100"));
            fw.append(String.format("%s,new_model_received,0,%s\n", ts, "path=global_model_mobile.pt"));
            fw.append(String.format("%s,local_weights_saved,1,%s\n", ts, "path=latest_weights.f32 sha=deadbeef"));
            fw.append(String.format("%s,fetch_meta_start,1,%s\n", ts, ""));
            fw.append(String.format("%s,fetch_meta_complete,1,%s\n", ts, "model_id=abc123"));
            fw.close();
            System.out.println("Wrote test logs to: " + logFile.getAbsolutePath());
        } catch (Exception e) {
            System.err.println("Failed to write: " + e.getMessage());
            e.printStackTrace();
            System.exit(2);
        }

        syncRounds();
    }

    private static int fetchServerRound() {
        int serverRound = -1;
        OkHttpClient client = new OkHttpClient();
        // Prefer environment variables so this utility can be run on desktop/CI.
        String edgeBase = System.getenv("EDGE_BASE_URL");
        if (edgeBase == null || edgeBase.isEmpty()) {
            edgeBase = "http://192.168.11.6:8001"; // fallback (sync with AppConfig default)
        }
        String authToken = System.getenv("SERVER_AUTH_TOKEN");
        if (authToken == null) authToken = "secret-token";

        String url = edgeBase.replaceAll("/+$", "") + "/status";

        Request request = new Request.Builder()
                .url(url)
                .addHeader("Authorization", "Bearer " + authToken)
                .build();

        try (Response response = client.newCall(request).execute()) {
            if (response.isSuccessful() && response.body() != null) {
                String responseBody = response.body().string();
                // Assuming the server returns a JSON object with a "round" field
                serverRound = Integer.parseInt(responseBody); // Simplified for example
            } else {
                System.err.println("Failed to fetch server round: " + response.code());
            }
        } catch (Exception e) {
            System.err.println("Error during server round fetch: " + e.getMessage());
        }

        return serverRound;
    }

    private static void syncRounds() {
        int serverRound = fetchServerRound();
        if (serverRound != -1) {
            System.out.println("Synchronizing to server round: " + serverRound);
            // Update local round logic here
        } else {
            System.err.println("Could not synchronize rounds due to server fetch failure.");
        }
    }
}
