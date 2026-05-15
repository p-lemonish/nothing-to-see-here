- https://www.notion.so/Kova-Hackathon-35ddbf9d2ee080ed9028fa463e97f818
- https://github.com/kova-labs/kova-wfb-rs
```bash
sudo apt update
sudo apt install libpcap-dev pkg-config build-essential

mkdir defence-hackathon

git clone https://github.com/svpcom/rtl8812au.git
cd rtl8812au
sudo ./dkms-install.sh
cd ..

git clone https://github.com/kova-labs/kova-wfb-rs.git
cd kova-wfb-rs
cargo build --examples
```
