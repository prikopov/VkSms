package ru.vksms.relay;

import android.Manifest;
import android.app.Activity;
import android.content.pm.PackageManager;
import android.os.Bundle;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;

public class MainActivity extends Activity {
    private TextView info;
    private Button grant;

    @Override public void onCreate(Bundle state) {
        super.onCreate(state);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (24 * getResources().getDisplayMetrics().density);
        layout.setPadding(pad, pad * 2, pad, pad);
        TextView title = new TextView(this);
        title.setText("VkSms USB");
        title.setTextSize(28);
        layout.addView(title);
        info = new TextView(this);
        info.setTextSize(16);
        layout.addView(info);
        grant = new Button(this);
        grant.setText("Разрешить отправку SMS");
        grant.setOnClickListener(v -> requestPermissions(new String[]{Manifest.permission.SEND_SMS}, 1));
        layout.addView(grant);
        setContentView(layout);
        refresh();
    }

    @Override protected void onResume() {
        super.onResume();
        refresh();
    }

    @Override public void onRequestPermissionsResult(int code, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(code, permissions, results);
        refresh();
    }

    private void refresh() {
        if (info == null) return;
        boolean allowed = checkSelfPermission(Manifest.permission.SEND_SMS) ==
                PackageManager.PERMISSION_GRANTED;
        info.setText(allowed
                ? "Разрешение SMS выдано. Подключите телефон по USB, разрешите отладку и проверьте телефон в программе Windows. При двух SIM выберите SIM по умолчанию для SMS."
                : "Выдайте разрешение SMS и подключите телефон к компьютеру по USB.");
        grant.setEnabled(!allowed);
    }
}
