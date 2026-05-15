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

---

# Linux Setup

# Drivers

If you want to run the WFB project on your linux host you need to install the patched driver for the RTL8812AU chip. To install the driver you need to have `dkms` and `iw` installed and run the following commands (refer to the `env-setup.sh` script for other missing dependencies):

```bash
git clone https://github.com/svpcom/rtl8812au.git
cd rtl8812au
sudo ./dkms-install.sh
```

*Note that [`dkms-install.sh`](http://dkms-install.sh) will disable ipv6 in `/etc/sysctl.conf.`*

Connect the USB WIFI adapter and find it’s interface from `iw dev`

Run `ethtool -i <INTERFACE>` and make sure the driver it’s using is rtl88xxau_wfb.

If not, run:
`sudo modprobe -r rtw88_8812au rtw88_usb`
`sudo modprobe 88XXau_wfb`

---

The Challenge
Build a tactical communication system for autonomous drones using radio mesh networking.

Introduction to the Challenge

In modern drone warfare and defence operations, reliable communication between autonomous unmanned systems is as critical as the autonomy itself. A single platform can carry its intelligence onboard, but it needs to be able to communicate battlefield insights back to the operators and other drones in the form of images and other data.

Off-the-shelf networks are often leaky, jammed, or compromised in contested environments – and the mesh networking modules existing today are too expensive to fit the economics of modern warfare. Kova Labs challenges you to build a tactical mesh communication system from the ground up, using low-power radio hardware. This is a real problem that directly impacts how autonomous systems coordinate in the field.

Challenge Description

Each team will receive up to three USB WI-FI adapters capable of transmitting and receiving raw IEEE 802.11 frames. Your task is to build a communication system where these devices can reliably exchange data in a mesh topology – think coordinating drone swarms, relaying sensor data, or establishing resilient command-and-control links.

You need to design the protocol, handle the data packing and transmission, and demonstrate a working mesh network. You are free to take this in any direction: optimizing throughput over unreliable data links, building encryption, creating a swarm coordination protocol, completely new abilities for swarms that mesh communication unlocks, or anything else you can imagine.

This challenge is divided into three layers, each of which have a prize associated with them. These layers are transmission, mesh, and application.

Transmission layer: Communication between two transceivers. Important aspects include bandwith, range, and reliability. Radiotap optimization, encryption, compression, etc. may also play a role here. All communications build upon this layer.
Mesh layer: Communication between multiple autonomous agents. Important aspects include network topology, data routing and distribution, and self-healing. This layer build on top of the transmission layer, building the base for applications.
Application layer: Extracting the maximal value from the mesh network. On this layer live the novel and innovative use cases of this mesh network, allowing the autonomous agents to coordinate together and achieve feats not possible otherwise. Even if you focus on a specific layer you should think about how your ideas and implementation effect the other layers.
We will award the prize to the team we see created the best and most innovative solution for that layer. We also have an overall first place prize for this challenge for the team that created the most impressive overall solution. We won't necessarily pick a separate team for each prize. If a team executes better than any other team on all fronts they can win all of the prizes. On the other hand if your team focuses fully on a specific layer, you will have better chances to win that layers prize and possibly the overall prize as well, given your submission's impressiveness stands out amongst the rest.

About the Company

Kova Labs is a Finnish defence technology company developing next-generation autonomous unmanned systems built to operate in the most demanding environments. The company gives machines a real-time understanding of the physical world, enabling unmanned systems to perceive, navigate, and act autonomously in complex and contested environments. Kova Labs brings deep expertise in embedded AI, computer vision, and drone autonomy, and is actively looking to recruit top talent from the defence and robotics community. By joining Junction DefenceHack, Kova Labs aims to discover innovative approaches to real-world defence challenges and connect with the next generation of engineers.

Insight
We are interested in creative approaches to efficient and reliable long range communication, resilient mesh topologies that handle jamming and spoofing, low-latency protocols suitable for real-time drone coordination, and any novel applications of tactical mesh networking you can think of.

Resources
What we're bringing

Each team will receive up to three packet injection capable USB WI-FI adapters. Kova Labs will provide Rust and C libraries for sending and receiving data over the WI-FI adapters, with Python bindings also available.

https://www.notion.so/Kova-Hackathon-35ddbf9d2ee080ed9028fa463e97f818 https://github.com/kova-labs/kova-wfb-rs

Mentors from Kova Labs with expertise in embedded systems, radio communications, and autonomous systems will be available throughout the weekend to help you get started and troubleshoot.

Further Instructions You can pick up the devices from the Kova Labs Booth.

Participants should deposit an ID/Driver’s Licence as collateral when receiving hardware.

ALL HARDWARE MUST BE RETURNED AT THE END OF THE EVENT!!!

Contact Persons Teemu Rautavalta - Co-founder and CTO Luukas Lohilahti – Co-Founder and CPO (Friday & Sunday) Touko Rautiainen – Co-founder and CEO (Friday & Sunday)
